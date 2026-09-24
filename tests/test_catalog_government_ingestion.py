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
import hashlib
import ast
import json
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from backend.catalog.contracts import stated_source_locator
from backend.catalog.government import ingest as ingest_module
from backend.catalog.government import normalize, projection, snapshot as snapshot_module
from backend.catalog.government import source as src
from backend.catalog.government import vocabulary as vocab
from backend.catalog.government.client import (DataGovClient, ResourceMetadata,
                                                schema_fingerprint)
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
from tests.run_factory import identity_kwargs
from backend.tools.registry import ToolRegistry

GOVERNMENT_PACKAGE = Path("backend/catalog/government")

#: Distinct from every JSON value, `null` included -- which is the whole point
#: of the `total_was_estimated` matrix below.
OMITTED = object()


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
    run = repository.create_message_and_run(
        conversation["id"], "go", {}, requested_by=user, idempotency_key=None,
        request_fingerprint="fp-go", **identity_kwargs(repository, conversation["id"]))["run"]
    claimed = repository.claim_run(run["id"], worker)
    return WorkerLease(claimed["id"], worker, int(claimed["attempt"]), claimed["lease_token"])


def client(**kwargs) -> DataGovClient:
    transport = kwargs.pop("transport", None) or FixtureTransport()
    kwargs.setdefault("page_limit", PINNED_PAGE_LIMIT)
    kwargs.setdefault("sleep_fn", lambda _seconds: None)
    return DataGovClient(transport, **kwargs)


def normalized(capture):
    """The reading of a whole capture, as the ingestor computes it.

    One pure function of the capture, so a test that builds a snapshot payload
    by hand carries exactly the summary an ingestion would have written.
    """
    return normalize.read_capture([record for _, record in capture.located_records()],
                                  resource_id=capture.resource_id)


def snapshot_payload_for(capture):
    return snapshot_module.snapshot_payload(capture, normalized(capture))


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
    metadata = snapshot_module.retrieval_metadata(capture, normalized(capture))
    rendered = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)
    assert len(rendered) <= src.MAX_RETRIEVAL_METADATA_CHARS
    assert metadata["page_checksums_inline"] is True
    assert [entry["sha256"] for entry in metadata["page_checksums"]] == \
        [page.body_sha256 for page in capture.pages]
    # And the inline list is dropped -- never truncated -- once a capture has
    # more pages than fit, while the chain still commits to every checksum.
    many = copy.copy(capture)
    object.__setattr__(many, "pages", capture.pages * 9)
    wide = snapshot_module.retrieval_metadata(many, normalized(capture))
    assert wide["page_checksums_inline"] is False and "page_checksums" not in wide
    assert len(json.dumps(wide, separators=(",", ":"), ensure_ascii=False)) \
        <= src.MAX_RETRIEVAL_METADATA_CHARS
    assert wide["page_chain_sha256"] != metadata["page_chain_sha256"]


def worst_case_normalization(capture):
    """Every refusal reason present, and the bounded id list full.

    The metadata budget has to hold for the worst snapshot, not the reviewed
    one -- and the reviewed capture has no refusals at all, so measuring
    against it would measure nothing.
    """
    reasons = sorted(normalize.GOVERNMENT_NORMALIZATION_REASONS)
    issues = tuple((str(900000 + index), reason) for index, reason in enumerate(reasons))
    issues += tuple((str(910000 + index), reasons[0])
                    for index in range(normalize.MAX_DURABLE_ISSUE_RECORDS + 2))
    return normalize.CaptureNormalization(
        contract=normalize.NORMALIZATION_CONTRACT,
        entries=normalized(capture).entries, issues=issues)


def widened(capture, page_count):
    wide = copy.copy(capture)
    pages = (capture.pages * (page_count // len(capture.pages) + 1))[:page_count]
    object.__setattr__(wide, "pages", pages)
    return wide


def test_the_inline_page_checksum_limit_holds_against_a_worst_case_summary():
    """The ceiling is a promise the durable bound can actually keep.

    Measured against the worst normalization summary this contract can produce,
    so the constant is not an optimistic number that a real capture would
    quietly exceed.
    """
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    worst = worst_case_normalization(capture)
    assert len(worst.issues_by_reason) == len(normalize.GOVERNMENT_NORMALIZATION_REASONS)
    assert len(worst.issue_records) == normalize.MAX_DURABLE_ISSUE_RECORDS

    metadata = snapshot_module.retrieval_metadata(
        widened(capture, src.MAX_INLINE_PAGE_CHECKSUMS), worst)
    assert metadata["page_checksums_inline"] is True
    assert len(metadata["page_checksums"]) == src.MAX_INLINE_PAGE_CHECKSUMS
    assert len(json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)) \
        <= src.MAX_RETRIEVAL_METADATA_CHARS


def test_the_inline_page_list_is_dropped_whole_and_never_truncated():
    """The degradation rule, stated and checked.

    Past the ceiling the per-page list goes away ENTIRELY -- a truncated list
    would be a snapshot claiming page provenance it does not carry -- while the
    chain digest, which commits to every checksum, is unchanged. The
    normalization summary is never what gets dropped: it decides whether the
    snapshot may answer a query at all.
    """
    capture = client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    worst = worst_case_normalization(capture)
    over = widened(capture, src.MAX_INLINE_PAGE_CHECKSUMS + 1)
    metadata = snapshot_module.retrieval_metadata(over, worst)
    assert metadata["page_checksums_inline"] is False
    assert "page_checksums" not in metadata
    assert metadata["page_chain_sha256"] == snapshot_module.page_chain_digest(
        tuple(page.body_sha256 for page in over.pages))
    assert metadata["normalization_issue_count"] == worst.issue_count
    assert metadata["normalization_issues"] == worst.durable_summary()["normalization_issues"]
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
        lease.run_id, snapshot_payload_for(capture),
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


def test_a_cancelled_ingestion_leaves_no_active_snapshot(repository, monkeypatch):
    # Rows are written in batches and cancellation is observed between them;
    # small batches make the interruption land part-way through the records.
    monkeypatch.setattr(ingest_module, "CATALOG_WRITE_BATCH_SIZE", 10)
    lease = leased_run(repository)

    def cancelled() -> bool:
        # Cancelled once five batches of raw records are durable.
        return len(repository.catalog_raw_records) >= 50

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
    payload = snapshot_payload_for(capture)
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
        lease.run_id, snapshot_payload_for(capture), worker_id=lease.worker_id,
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
        first.run_id, snapshot_payload_for(capture), worker_id=first.worker_id,
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
    metadata = snapshot_module.retrieval_metadata(capture, normalized(capture))
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
        lease.run_id, snapshot_payload_for(pending_capture),
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


def test_the_projection_is_still_not_a_tool_and_the_registration_is_a_wrapper():
    """PR2's query layer stayed a plain class; PR3 wrapped it, and only that.

    The assertion here was "no tool is registered at all". PR3 registers one,
    so what is worth pinning is that the registration did not turn the query
    layer INTO a tool: `GovernmentCatalogProjection` and `GovernmentCatalogQuery`
    still have no operations mapping, no schema and no required scope, and the
    Tool protocol lives entirely in `backend/tools/government_vehicle.py`.

    `backend/tools/registry.py` still names no source: the framework knows
    nothing about the Government catalog, exactly as it knew nothing before.
    """
    from backend.catalog.government.query import GovernmentCatalogQuery

    registry = Path("backend/tools/registry.py").read_text(encoding="utf-8")
    assert "government" not in registry.lower()
    for reader in (GovernmentCatalogProjection, GovernmentCatalogQuery):
        for attribute in ("operations", "input_schema", "output_schema",
                          "required_scope", "mode"):
            assert not hasattr(reader, attribute), (reader, attribute)
    # The worker registers the TOOL, not the reader: a plan can name
    # `catalog.government_vehicle`, and nothing else in this package.
    #
    # CODE-2 made the registration conditional on
    # `MILO_ENABLE_CATALOG_EXECUTION`, so the enabled branch is what this pins.
    # What the flag cannot change is WHICH object gets registered: disabled it
    # is nothing at all, enabled it is the Tool wrapper and never the reader.
    worker = Path("backend/worker/main.py").read_text(encoding="utf-8")
    # The registration is PINNED to the snapshot the Government preparation
    # stage resolved after the lease, so every read of the run answers from
    # one immutable snapshot; it is still the Tool wrapper, never the reader.
    collapsed = " ".join(worker.split())
    assert ("tools = ToolRegistry( [GovernmentVehicleTool(repo, snapshot_key=preparation.snapshot_key)] "
            "if government_read_enabled else [])") in collapsed
    assert "GovernmentCatalogProjection" not in worker
    assert "GovernmentCatalogQuery" not in worker
    assert "DataGovClient" not in worker
    # And no production entrypoint constructs a transport, so a chat run still
    # cannot reach `data.gov.il` -- the Tool reads durable rows only.
    assert "HttpsDataGovTransport" not in worker


def test_only_the_reviewed_pr3_seams_import_the_government_package():
    """PR2 built the capability; Catalog PR3 connects it -- at NAMED seams.

    PR2 asserted here that nothing outside the package imported it at all.
    PR3 makes that false on purpose, so the assertion becomes the list of
    places it is now reachable from -- and stays a test failure if another
    module starts importing the Government catalog without a reviewer noticing.

    `backend/testing/r5_proof/government.py` reads the shared code/label
    vocabulary, which is the point of moving it: ONE definition of what a
    register code means, selected down to the subset R5 reviewed.
    """
    allowed = {
        "backend/catalog/government",
        "backend/testing/government_capture.py",
        "backend/testing/r5_proof/government.py",
        # PR3: the Tool that exposes the reviewed query layer, the worker
        # wiring that registers it, the production evidence-mapper allowlist
        # that names its one evidence-bearing operation, and the plan policy
        # that carries a source-first rule for it. Nothing else in `backend/`
        # may name the Government catalog.
        "backend/tools/government_vehicle.py",
        "backend/worker/main.py",
        # Catalog PR3's trusted promotion path names the ONE registered
        # Government operation, so it knows which tool result carries a
        # candidate. It imports two constants and no capture code.
        "backend/catalog/pipeline.py",
        "backend/engines/swarm_v2/evidence_mapping.py",
        "backend/engines/swarm_v2/validation.py",
        # CODE-1: the ONE operator-invoked capture entrypoint. It is the first
        # module in this repository that constructs the live capture path, and
        # it is orchestration only -- it refuses by default, it is gated on
        # `MILO_ENABLE_CATALOG_EXECUTION`, and `tests/test_catalog_operator_capture.py`
        # holds it to that. It is NOT a worker, a route, a tool or a schedule:
        # the assertions below and in that module are what keep it one.
        "backend/catalog/operator_capture.py",
        # CODE-3: the bounded READ-ONLY review layer. It names the WLTP resource
        # constant and reads through `GovernmentCatalogQuery`, which is the
        # existing bounded database-side reader -- it constructs no transport,
        # no `DataGovClient` and no live capture path of any kind, and
        # `tests/test_catalog_review_surface.py` proves that by making both
        # constructions an outright error during a request. It is a read: no
        # lease, no write, no flag.
        "backend/catalog/review.py",
        # CODE-3, test-only: the durable catalog the isolated E2E stack reads.
        # It lands the committed R5 capture fixtures through the real ingestion
        # path with `FixtureTransport`, exactly as `government_capture.py`
        # above does for the offline suites. Imported only by
        # `backend/testing/e2e_app.py`, which is never deployed.
        "backend/testing/catalog_review_seed.py",
        # Scoped catalog PR3, test-only: a PREPARED Mapping Plan for the offline
        # suites and the enabled E2E stack. It lands committed R5 rows as the
        # scoped page a per-marque capture reads, through the real ingestion
        # path with `FixtureTransport` -- no socket, no live transport -- and
        # prepares the plan through the repository's own lease-guarded write.
        # Imported by tests and by `backend/testing/e2e_app.py` only.
        "backend/testing/work_scope_seed.py",
        # Scoped catalog PR1: the WorkScope contract pins the Government SOURCE
        # a plan reads (package and resource constants) and the model-year
        # bounds the register reading applies. Two constant modules, and
        # nothing else: no client, no transport, no capture, no query.
        "backend/catalog/scope/contract.py",
        # Scoped catalog PR2: preparing one plan revision -- a SCOPED refresh
        # per verified marque, read back by exact key. It constructs no
        # transport: its only caller is `operator_capture.py` (listed above),
        # which hands it the client, inside the capture job.
        "backend/catalog/scope/preparation.py",
    }
    # An IMPORT is the seam this guards. A bare occurrence of the dotted name
    # is not: `backend/testing/memory_repository.py` has to know the capture
    # OPERATION MARKER (`catalog.government.capture`) to mirror the V3 trigger's
    # operator-capture rule, and knowing a string is not reaching into the
    # package. So the check is the import graph, read from the parsed module.
    for path in sorted(Path("backend").rglob("*.py")):
        text = str(path)
        if any(text.startswith(prefix) for prefix in allowed):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [name for name in imported
                    if name == "backend.catalog.government"
                    or name.startswith("backend.catalog.government.")], text


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


# =============================================================================
# REGRESSIONS — five defects found in review of fce72a4a
# =============================================================================
#
# Every test in this section fails against that head. They are grouped by the
# defect they pin rather than by module, so a reviewer can read one block and
# see the whole property.


# --- 1. the pinned CKAN package is enforced BEFORE egress --------------------

def test_a_package_that_is_not_the_pinned_one_never_reaches_the_network():
    """The package id is a SOURCE IDENTITY, so it is checked before it is sent.

    `package_show` used to put a caller's package id straight into the query
    string and only compare it against the pinned one AFTER the response came
    back -- so a wrong value was a request to `data.gov.il` that this package
    had already decided it would refuse.
    """
    transport = FixtureTransport()
    refuses("GOV_PACKAGE_NOT_ALLOWED", client(transport=transport).package_show,
            src.WLTP_RESOURCE_ID, package_id="some-other-dataset")
    assert transport.calls == []


def test_no_capture_or_ingestion_can_name_another_package(repository):
    """Every public entry point applies the same allowlist, before egress and
    before a single durable write."""
    transport = FixtureTransport()
    refuses("GOV_PACKAGE_NOT_ALLOWED", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, package_id="some-other-dataset",
            query=dict(PINNED_QUERY))
    assert transport.calls == []

    lease = leased_run(repository)
    ingestor = GovernmentCatalogIngestor(repository, lease,
                                         client=client(transport=transport))
    with pytest.raises(GovernmentSourceError) as failure:
        ingestor.ingest_resource(src.WLTP_RESOURCE_ID, package_id="some-other-dataset",
                                 query=dict(PINNED_QUERY))
    assert failure.value.reason_code == "GOV_PACKAGE_NOT_ALLOWED"
    assert transport.calls == []
    assert repository.catalog_snapshots == {}
    assert repository.catalog_raw_records == {}
    assert repository.catalog_candidates == {}


def test_the_package_allowlist_is_closed_and_reasons_are_static():
    assert src.ALLOWED_PACKAGE_IDS == frozenset({src.CKAN_PACKAGE_ID})
    assert src.require_allowed_package(src.CKAN_PACKAGE_ID) == src.CKAN_PACKAGE_ID
    for rejected in ("", "  ", "degem-rechev-wltp-copy", "DEGEM-RECHEV-WLTP", None):
        refuses("GOV_PACKAGE_NOT_ALLOWED", src.require_allowed_package, rejected)
    assert "GOV_PACKAGE_NOT_ALLOWED" in src.GOVERNMENT_SOURCE_REASONS


# --- 2. `total_was_estimated` is validated by TYPE and value ------------------

#: (label, the JSON value at `total_was_estimated`, accepted?)
#:
#: Omission is ACCEPTED and documented: CKAN omits the key entirely on
#: responses that did not estimate, and refusing an absent key would refuse
#: every such response. A PRESENT key must be the JSON boolean `false`; `true`
#: is an estimate and is refused as one; every other type and value is a
#: malformed response and is refused as that -- `1`, `"true"` and `null` used
#: to sail straight through an `is True` check.
ESTIMATED_TOTAL_CASES = (
    ("missing", OMITTED, True, None),
    ("false", False, True, None),
    ("true", True, False, "GOV_TOTAL_ESTIMATED"),
    ("one", 1, False, "GOV_TOTAL_ESTIMATION_INVALID"),
    ("zero", 0, False, "GOV_TOTAL_ESTIMATION_INVALID"),
    ("string true", "true", False, "GOV_TOTAL_ESTIMATION_INVALID"),
    ("string false", "false", False, "GOV_TOTAL_ESTIMATION_INVALID"),
    ("null", None, False, "GOV_TOTAL_ESTIMATION_INVALID"),
    ("object", {}, False, "GOV_TOTAL_ESTIMATION_INVALID"),
    ("array", [], False, "GOV_TOTAL_ESTIMATION_INVALID"),
)


@pytest.mark.parametrize("label,value,accepted,reason", ESTIMATED_TOTAL_CASES,
                         ids=[case[0] for case in ESTIMATED_TOTAL_CASES])
def test_the_estimated_total_flag_is_a_strict_json_boolean(label, value, accepted, reason,
                                                           repository):
    document = page_document(0)
    if value is OMITTED:
        document["result"].pop("total_was_estimated", None)
    else:
        document["result"]["total_was_estimated"] = value
    transport = FixtureTransport(bodies={0: encode(document)})
    reader = client(transport=transport)
    if accepted:
        capture = reader.capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
        assert capture.reported_total == PINNED_TOTAL
        return
    refuses(reason, reader.capture_resource, src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    # A malformed response never reaches pagination or persistence.
    assert repository.catalog_snapshots == {}


def test_a_malformed_estimation_flag_is_refused_before_the_next_page_is_fetched():
    """It is a property of the FIRST page, so the capture stops there."""
    transport = FixtureTransport(bodies={0: page_with(0, total_was_estimated="true")})
    refuses("GOV_TOTAL_ESTIMATION_INVALID", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    offsets = [call[1].get("offset") for call in transport.calls
               if call[0] == src.DATASTORE_SEARCH]
    assert offsets == ["0"]


# --- 3 & 4. exact snapshot pinning, and ordering parity ----------------------

def activate_empty_snapshot(repository, lease, *, content, resource=None, family="government",
                            activate=True, state=None, activated_at=None, metadata=None):
    """One snapshot of zero records, so a test can make many of them cheaply.

    `declared_record_count` is 0, so activation succeeds immediately: what these
    tests are about is WHICH snapshot a lookup returns, not what is in it.
    """
    payload = {"source_family": family,
               "resource_id": resource or src.WLTP_RESOURCE_ID,
               "upstream_version": "2026.09.1", "upstream_version_kind": "dataset_version",
               "content_sha256": content, "retrieved_at": "2026-09-14T16:11:13.272Z",
               "declared_record_count": 0,
               "retrieval_metadata": metadata if metadata is not None else {
                   "normalization_contract": "gov.wltp.normalize.1",
                   "normalized_record_count": 0, "normalization_issue_count": 0,
                   "normalization_issues": [], "normalization_issue_records": []}}
    keys = {"worker_id": lease.worker_id, "attempt": lease.attempt,
            "lease_token": lease.lease_token}
    row = repository.record_catalog_snapshot(lease.run_id, payload, **keys)
    if activate:
        repository.activate_catalog_snapshot(
            lease.run_id, {"snapshot_id": row["id"], **({"validation_state": state} if state else {})},
            **keys)
    stored = repository.catalog_snapshots[row["snapshot_key"]]
    if activated_at is not None and stored["activated_at"] is not None:
        stored["activated_at"] = activated_at
    return stored


def test_an_older_pinned_active_snapshot_is_reachable_past_the_listing_bound(repository):
    """Pinning a snapshot is an EXACT lookup, not a search of the newest page.

    The bounded listing exists to choose the newest snapshot. Reusing it to
    resolve an explicit `snapshot_key` made every active snapshot older than
    the bound unreachable -- a silent "unknown snapshot" for a row that is
    right there, active, in the table.
    """
    lease = leased_run(repository)
    listing_bound = repository.MAX_CATALOG_SNAPSHOT_ROWS
    oldest = activate_empty_snapshot(repository, lease, content="a" * 64,
                                     activated_at="2020-01-01T00:00:00+00:00")
    for index in range(listing_bound + 10):
        activate_empty_snapshot(repository, lease, content=f"{index:064x}",
                                activated_at=f"2026-01-01T00:00:{index % 60:02d}+00:00")
    assert len(repository.list_active_catalog_snapshots("government")) == listing_bound
    assert oldest["snapshot_key"] not in {
        row["snapshot_key"] for row in repository.list_active_catalog_snapshots("government")}

    found = repository.find_active_catalog_snapshot(
        "government", src.WLTP_RESOURCE_ID, oldest["snapshot_key"])
    assert found is not None and found["snapshot_key"] == oldest["snapshot_key"]
    view = GovernmentCatalogProjection(repository, snapshot_key=oldest["snapshot_key"])
    assert view.dataset_metadata().snapshot_key == oldest["snapshot_key"]


@pytest.mark.parametrize("label,kwargs", [
    ("a pending snapshot", {"activate": False}),
    ("a failed snapshot", {"state": "failed"}),
    ("another resource", {"resource": src.QUANTITY_RESOURCE_ID}),
])
def test_the_exact_lookup_returns_only_an_active_snapshot_of_that_family_and_resource(
        repository, label, kwargs):
    lease = leased_run(repository)
    row = activate_empty_snapshot(repository, lease, content="b" * 64, **kwargs)
    assert repository.find_active_catalog_snapshot(
        "government", src.WLTP_RESOURCE_ID, row["snapshot_key"]) is None
    assert repository.find_active_catalog_snapshot(
        "manufacturer", src.WLTP_RESOURCE_ID, row["snapshot_key"]) is None


def test_active_snapshots_are_ordered_identically_by_both_repositories(repository):
    """`activated_at` DESC, then `snapshot_key` ASC -- in BOTH implementations.

    The memory repository sorted the whole tuple in reverse, which reverses the
    TIEBREAK too. Every snapshot below shares one activation instant, so the
    order is decided entirely by the tiebreak and the disagreement is visible.
    """
    lease = leased_run(repository)
    keys = sorted(activate_empty_snapshot(repository, lease, content=f"{index:064x}",
                                          activated_at="2026-09-15T12:00:00+00:00")
                  ["snapshot_key"] for index in range(5))
    listed = [row["snapshot_key"]
              for row in repository.list_active_catalog_snapshots("government")]
    assert listed == keys, "ties must break on snapshot_key ASCENDING"

    newer = activate_empty_snapshot(repository, lease, content="c" * 64,
                                    activated_at="2026-09-16T12:00:00+00:00")
    listed = [row["snapshot_key"]
              for row in repository.list_active_catalog_snapshots("government")]
    assert listed == [newer["snapshot_key"], *keys], "newest activation first"


# --- 5. an active snapshot never silently loses a normalized row -------------

#: The drivetrain label each `hanaa_cd` is paired with, and its opposite. Used
#: to give a real row a real contradiction: the code stays exactly as the
#: register wrote it and the label becomes the OTHER code's label, so what the
#: test exercises is the contract rather than a fixture written to fail.
DRIVETRAIN_LABEL_SWAP = {1: "4X4", 3: "4X2"}


def contradictory_capture_bodies(count=1):
    """The committed page one, with `count` rows given a real contradiction.

    The label is chosen from the row's OWN code, so every selected row is
    genuinely contradictory whatever it happened to state -- writing one fixed
    label would silently be correct for some rows and prove nothing.
    """
    document = page_document(0)
    broken = []
    for record in document["result"]["records"][:count]:
        record["hanaa_nm"] = DRIVETRAIN_LABEL_SWAP[record["hanaa_cd"]]
        broken.append(str(record["_id"]))
    return {0: encode(document)}, broken


def durable_normalization(repository, snapshot_key):
    """The normalization summary the SNAPSHOT ROW carries, not the report's."""
    metadata = repository.catalog_snapshots[snapshot_key]["retrieval_metadata"]
    return {"contract": metadata.get("normalization_contract"),
            "normalized": metadata.get("normalized_record_count"),
            "issues": metadata.get("normalization_issue_count"),
            "reasons": {entry["reason"]: entry["count"]
                        for entry in metadata.get("normalization_issues") or []},
            "records": tuple(metadata.get("normalization_issue_records") or ())}


def test_an_active_snapshot_records_its_normalization_gap_durably(repository):
    """A raw row with no candidate must leave a DURABLE trace.

    Before this, an unreadable row was reported once, in memory, by the
    ingestion that happened to write it -- and the snapshot went active holding
    a raw record that no candidate and no stored fact accounted for.
    """
    lease = leased_run(repository)
    bodies, broken = contradictory_capture_bodies()
    report = ingest(repository, lease, transport=FixtureTransport(bodies=bodies))

    durable = durable_normalization(repository, report.snapshot_key)
    assert durable["issues"] == 1
    assert durable["reasons"] == {"GOV_NORM_LABEL_CONTRADICTION": 1}
    assert durable["records"] == tuple(broken)
    assert durable["normalized"] == PINNED_TOTAL - 1

    # The arithmetic that makes "silently" impossible: every stored raw record
    # is either a candidate or a durably counted issue.
    snapshot = repository.catalog_snapshots[report.snapshot_key]
    candidates = [row for row in repository.catalog_candidates.values()
                  if row["snapshot_id"] == snapshot["id"]]
    assert snapshot["stored_record_count"] - len(candidates) == durable["issues"]


def test_a_contradiction_never_silently_becomes_the_newest_usable_projection(repository):
    """The last usable snapshot keeps answering.

    A newer capture that is raw-complete but semantically incomplete must not
    displace it, and must not be readable by accident: reading it takes an
    explicit acknowledgement, and then the gap travels with every answer.
    """
    lease = leased_run(repository)
    good = ingest(repository, lease)
    bodies, _broken = contradictory_capture_bodies()
    incomplete = ingest(repository, lease, transport=FixtureTransport(bodies=bodies))
    assert incomplete.snapshot_key != good.snapshot_key
    assert incomplete.activated is True          # the RAW capture is complete

    # The default read still answers from the last USABLE snapshot.
    assert GovernmentCatalogProjection(repository).dataset_metadata().snapshot_key == \
        good.snapshot_key
    # Pinning the incomplete one is refused rather than silently answered.
    with pytest.raises(GovernmentProjectionError) as failure:
        GovernmentCatalogProjection(repository,
                                    snapshot_key=incomplete.snapshot_key).dataset_metadata()
    assert failure.value.reason_code == "GOV_PROJECTION_SNAPSHOT_INCOMPLETE"
    # And an explicit acknowledgement exposes the gap on every answer.
    view = GovernmentCatalogProjection(repository, snapshot_key=incomplete.snapshot_key,
                                       allow_incomplete=True)
    provenance = view.dataset_metadata()
    assert provenance.snapshot_key == incomplete.snapshot_key
    assert provenance.normalization_issue_count == 1
    assert provenance.normalization_issues == {"GOV_NORM_LABEL_CONTRADICTION": 1}
    assert view.list_variants("טויוטה", "RAV4").provenance.normalization_issue_count == 1


def test_replay_and_cross_run_reuse_report_the_durable_truth(repository):
    """The gap is a property of the SNAPSHOT, so every report of it agrees."""
    lease = leased_run(repository)
    bodies, broken = contradictory_capture_bodies(count=2)
    first = ingest(repository, lease, transport=FixtureTransport(bodies=bodies))
    assert (first.normalization_issue_count, first.normalized_record_count) == \
        (2, PINNED_TOTAL - 2)

    replay = ingest(repository, lease, transport=FixtureTransport(bodies=bodies))
    assert replay.snapshot_key == first.snapshot_key
    assert replay.normalization_issue_count == first.normalization_issue_count
    assert replay.normalization_issues == first.normalization_issues
    assert replay.normalization_issue_records == tuple(broken)

    later = leased_run(repository, worker="worker-2")
    reused = GovernmentCatalogIngestor(
        repository, later, client=client(transport=FixtureTransport(bodies=bodies))
    ).ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert reused.reused_existing is True
    assert reused.snapshot_key == first.snapshot_key
    assert reused.normalization_issue_count == first.normalization_issue_count
    assert reused.normalization_issues == first.normalization_issues


def test_a_clean_capture_states_a_clean_normalization_contract(repository, ingested):
    _, report = ingested
    assert report.normalization_contract == "gov.wltp.normalize.1"
    assert report.normalization_issue_count == 0
    assert report.normalized_record_count == PINNED_TOTAL
    durable = durable_normalization(repository, report.snapshot_key)
    assert durable == {"contract": "gov.wltp.normalize.1", "normalized": PINNED_TOTAL,
                       "issues": 0, "reasons": {}, "records": ()}
    assert GovernmentCatalogProjection(repository).dataset_metadata() \
        .normalization_issue_count == 0


def quantity_transport():
    """The committed pages, re-pointed at the QUANTITY resource.

    The rows are WLTP-shaped -- there is no committed capture of the quantity
    resource -- and that is precisely the point: this test is about the
    RESOURCE CONTRACT, which says those rows are stored raw and never read for
    identity, whatever they happen to contain.
    """
    bodies = {}
    for offset in (0, 100, 200):
        bodies[offset] = page_with(offset, resource_id=src.QUANTITY_RESOURCE_ID)
    return FixtureTransport(bodies=bodies)


def test_the_quantity_resource_is_ingested_raw_only_and_never_normalized(repository):
    """Allowlisted for CAPTURE, deliberately unread for IDENTITY.

    Its rows are durable and its snapshot is a legitimate active capture; what
    it is NOT is a source of candidate identities, so it produces no candidate,
    no normalization issue, and no tree.
    """
    lease = leased_run(repository)
    ingestor = GovernmentCatalogIngestor(repository, lease,
                                         client=client(transport=quantity_transport()))
    report = ingestor.ingest_resource(src.QUANTITY_RESOURCE_ID, query=dict(PINNED_QUERY))

    assert report.activated is True
    assert report.stored_record_count == PINNED_TOTAL
    assert report.candidate_count == 0
    assert report.normalization_contract == "raw_only"
    assert report.normalization_issue_count == 0
    assert durable_normalization(repository, report.snapshot_key)["contract"] == "raw_only"
    assert repository.catalog_candidates == {}

    with pytest.raises(GovernmentProjectionError) as failure:
        GovernmentCatalogProjection(repository,
                                    resource_id=src.QUANTITY_RESOURCE_ID).dataset_metadata()
    assert failure.value.reason_code == "GOV_PROJECTION_RESOURCE_NOT_NORMALIZED"


def test_a_snapshot_read_under_another_contract_is_not_reused(repository):
    """A replay must RECONSTRUCT the stored gap, not assume it.

    If the stored summary and the freshly computed one disagree, the snapshot
    was read under different rules -- a vocabulary entry changed, or another
    release wrote it -- and reusing it as though this code had read it would be
    exactly the drift the summary exists to prevent.
    """
    lease = leased_run(repository)
    report = ingest(repository, lease)
    stored = repository.catalog_snapshots[report.snapshot_key]["retrieval_metadata"]
    stored["normalization_contract"] = "gov.wltp.normalize.0"

    with pytest.raises(GovernmentIngestionError) as failure:
        ingest(repository, lease)
    assert failure.value.reason_code == "GOV_SNAPSHOT_NORMALIZATION_DRIFT"
    later = leased_run(repository, worker="worker-2")
    with pytest.raises(GovernmentIngestionError):
        ingest(repository, later)
    # A refused replay leaves the stored snapshot exactly as it was.
    assert repository.catalog_snapshots[report.snapshot_key]["retrieval_metadata"] is stored
    assert len(repository.catalog_raw_records) == PINNED_TOTAL


@pytest.mark.parametrize("label,metadata,reason", [
    ("a raw-only capture", {"normalization_contract": "raw_only",
                            "normalized_record_count": 0, "normalization_issue_count": 0,
                            "normalization_issues": [], "normalization_issue_records": []},
     "GOV_PROJECTION_RESOURCE_NOT_NORMALIZED"),
    ("a snapshot with no stated reading", {"page_count": 1},
     "GOV_PROJECTION_SNAPSHOT_NOT_READ"),
    # A raw-only summary is held to the same completeness as any other: an
    # omitted list is metadata this code did not write, not an empty one.
    ("a half-stated raw-only summary", {"normalization_contract": "raw_only",
                                        "normalized_record_count": 0,
                                        "normalization_issue_count": 0},
     "GOV_PROJECTION_SNAPSHOT_STATE_INVALID"),
    ("a raw-only summary claiming a reading",
     {"normalization_contract": "raw_only", "normalized_record_count": 3,
      "normalization_issue_count": 0, "normalization_issues": [],
      "normalization_issue_records": []},
     "GOV_PROJECTION_SNAPSHOT_STATE_INVALID"),
])
def test_an_acknowledgement_never_reaches_a_snapshot_that_states_no_identities(
        repository, label, metadata, reason):
    """`allow_incomplete` acknowledges a COUNTED gap, and nothing else.

    A raw-only resource states no vehicle identities at all, and a snapshot
    that records no reading cannot say what it is missing -- neither is an
    incomplete answer a caller could acknowledge, so neither becomes readable.
    """
    lease = leased_run(repository)
    row = activate_empty_snapshot(repository, lease, content="d" * 64, metadata=metadata)
    for acknowledged in (False, True):
        view = GovernmentCatalogProjection(repository, snapshot_key=row["snapshot_key"],
                                           allow_incomplete=acknowledged)
        with pytest.raises(GovernmentProjectionError) as failure:
            view.dataset_metadata()
        assert failure.value.reason_code == reason
    assert projection.ACKNOWLEDGEABLE_REFUSALS == {"GOV_PROJECTION_SNAPSHOT_INCOMPLETE"}


def test_an_incomplete_snapshot_with_no_usable_predecessor_refuses_rather_than_answers(
        repository):
    """With nothing usable behind it, the read is a refusal -- never a quiet
    answer from the incomplete capture."""
    lease = leased_run(repository)
    bodies, _broken = contradictory_capture_bodies()
    only = ingest(repository, lease, transport=FixtureTransport(bodies=bodies))
    assert only.activated is True and only.normalization_issue_count == 1
    with pytest.raises(GovernmentProjectionError) as failure:
        GovernmentCatalogProjection(repository).dataset_metadata()
    assert failure.value.reason_code == "GOV_PROJECTION_SNAPSHOT_INCOMPLETE"


def test_an_ambiguous_reading_is_a_candidate_and_never_a_normalization_issue(repository):
    """An unknown-but-not-contradictory code still produces a candidate.

    `ambiguous` is a first-class answer in the Catalog PR1 vocabulary, so it is
    not a gap: the row IS read, the reading says what it could not settle, and
    the snapshot stays usable.
    """
    lease = leased_run(repository)
    document = page_document(0)
    document["result"]["records"][0].update({"hanaa_cd": 99, "hanaa_nm": "משהו אחר"})
    report = ingest(repository, lease,
                    transport=FixtureTransport(bodies={0: encode(document)}))
    assert report.normalization_issue_count == 0
    assert report.candidate_count == PINNED_TOTAL
    assert report.candidate_status_counts == {"candidate": PINNED_TOTAL - 1, "ambiguous": 1}
    # Still usable, and the ambiguity is visible on the variant itself.
    view = GovernmentCatalogProjection(repository)
    assert view.dataset_metadata().normalization_issue_count == 0
    ambiguous = [item for item in view.list_variants("טויוטה", "RAV4", limit=200).items
                 if item.status == "ambiguous"]
    assert len(ambiguous) == 1 and ambiguous[0].unresolved_dimensions == ("drivetrain",)


# =============================================================================
# REGRESSIONS — trust-boundary findings from the review of fcc567b
# =============================================================================


# --- 1. metadata is never caller-authored -----------------------------------

def test_capture_resource_accepts_no_caller_authored_metadata():
    """`ResourceMetadata` is a RESULT of the validated path, never an input.

    `capture_resource` used to take a `metadata=` override that it checked only
    for package and resource equality -- so a caller could hand it a publisher,
    a source version and a response digest that `package_show` had never
    validated, and every page and every durable row would then be pinned to
    them. The parameter had no caller anywhere; it is gone.
    """
    import inspect

    parameters = inspect.signature(DataGovClient.capture_resource).parameters
    assert "metadata" not in parameters
    assert set(parameters) == {"self", "resource_id", "package_id", "query"}
    with pytest.raises(TypeError):
        client().capture_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY),
                                  metadata=object())


def test_forged_metadata_with_an_allowed_package_and_resource_cannot_capture(repository):
    """The exact bypass: allowed ids, everything else invented.

    `ResourceMetadata` carries the publisher, the source version and the
    digest of the metadata response -- the three things `package_show`
    validates. The override checked only the package and the resource, so a
    caller could satisfy it while inventing all three, and every page, every
    raw record and the snapshot itself would then be pinned to provenance no
    response ever stated.
    """
    forged = ResourceMetadata(
        package_id=src.CKAN_PACKAGE_ID, resource_id=src.WLTP_RESOURCE_ID,
        publisher="ministry_of_someone_else", dataset_title="Forged",
        upstream_version="2099.12.31", upstream_version_kind="dataset_version",
        retrieved_at="2099-12-31T00:00:00Z", metadata_response_sha256="f" * 64)
    transport = FixtureTransport()
    with pytest.raises(TypeError):
        client(transport=transport).capture_resource(
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY), metadata=forged)
    assert transport.calls == []
    assert repository.catalog_snapshots == {}


def test_every_capture_reads_its_metadata_through_package_show():
    transport = FixtureTransport()
    capture = client(transport=transport).capture_resource(
        src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert transport.calls[0][0] == src.PACKAGE_SHOW
    assert capture.metadata.publisher == src.GOVERNMENT_PUBLISHER
    assert capture.metadata.metadata_response_sha256 == \
        hashlib.sha256(capture_fixtures.package_body()).hexdigest()


FORGED_PACKAGE_METADATA = (
    ("a forged publisher", "organization", {"name": "ministry_of_someone_else"},
     "GOV_PUBLISHER_MISMATCH"),
    ("a forged package identity", "name", "degem-rechev-wltp-copy",
     "GOV_PACKAGE_IDENTITY_MISMATCH"),
)


@pytest.mark.parametrize("label,field,value,reason", FORGED_PACKAGE_METADATA,
                         ids=[case[0] for case in FORGED_PACKAGE_METADATA])
def test_forged_dataset_metadata_produces_no_capture_and_no_snapshot(
        repository, label, field, value, reason):
    """The allowlist decides WHICH dataset; `package_show` decides what it IS.

    An allowed package and an allowed resource are necessary and not
    sufficient: the publisher and the identity the response states are checked
    too, before a single page is requested.
    """
    package = json.loads(capture_fixtures.package_body().decode("utf-8"))
    package["result"][field] = value
    transport = FixtureTransport(bodies={"package": encode(package)})
    refuses(reason, client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert [call[0] for call in transport.calls] == [src.PACKAGE_SHOW]

    lease = leased_run(repository)
    with pytest.raises(GovernmentSourceError):
        GovernmentCatalogIngestor(repository, lease, client=client(transport=transport)) \
            .ingest_resource(src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert repository.catalog_snapshots == {}
    assert repository.catalog_raw_records == {}


@pytest.mark.parametrize("label,version", [
    ("a version the evidence contract would refuse", "not a version"),
    ("an empty version", "   "),
])
def test_a_forged_source_version_produces_no_capture(label, version):
    package = json.loads(capture_fixtures.package_body().decode("utf-8"))
    for resource in package["result"]["resources"]:
        resource.pop("revision_id", None)
        resource["last_modified"] = version
    transport = FixtureTransport(bodies={"package": encode(package)})
    refuses("GOV_RESOURCE_UNVERSIONED", client(transport=transport).capture_resource,
            src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    assert [call[0] for call in transport.calls] == [src.PACKAGE_SHOW]


# --- 2. durable normalization state is PARSED, not assumed -------------------

def stored_metadata(repository, snapshot_key):
    return repository.catalog_snapshots[snapshot_key]["retrieval_metadata"]


def ingested_then_metadata(repository, *, removed=(), **overrides):
    """One real ingestion whose stored summary is then edited to a bad state."""
    lease = leased_run(repository)
    report = ingest(repository, lease)
    metadata = stored_metadata(repository, report.snapshot_key)
    for field in removed:
        metadata.pop(field, None)
    metadata.update(overrides)
    return report


CONTRADICTION = "GOV_NORM_LABEL_CONTRADICTION"

#: Durable summaries that LOOK fine to an equality check and are not.
#:
#: Every one of these passed the old `_usability`, which asked only whether the
#: contract string matched and whether one integer was zero -- so a snapshot
#: whose recorded reading was missing, mistyped, internally inconsistent or
#: simply invented answered queries as though it were whole.
MALFORMED_NORMALIZATION_STATES = (
    ("a missing normalized count", {"removed": ("normalized_record_count",)}),
    ("a missing issue count", {"removed": ("normalization_issue_count",)}),
    ("a missing issues list", {"removed": ("normalization_issues",)}),
    ("a negative normalized count", {"normalized_record_count": -1}),
    ("a negative issue count", {"normalization_issue_count": -1}),
    ("a string normalized count", {"normalized_record_count": "233"}),
    ("a string issue count", {"normalization_issue_count": "0"}),
    ("a boolean issue count", {"normalization_issue_count": True}),
    ("a list normalized count", {"normalized_record_count": []}),
    ("an object normalized count", {"normalized_record_count": {}}),
    ("counts that do not sum to the stored rows", {"normalized_record_count": 200}),
    ("an issues list that is not a list", {"normalization_issues": {CONTRADICTION: 1}}),
    ("an issue entry that is not an object", {"normalization_issue_count": 1,
                                              "normalized_record_count": 232,
                                              "normalization_issues": [CONTRADICTION],
                                              "normalization_issue_records": ["38683"]}),
    ("an issue entry with unexpected keys", {"normalization_issue_count": 1,
                                             "normalized_record_count": 232,
                                             "normalization_issues": [
                                                 {"reason": CONTRADICTION, "count": 1,
                                                  "note": "why"}],
                                             "normalization_issue_records": ["38683"]}),
    ("an unknown reason", {"normalization_issue_count": 1, "normalized_record_count": 232,
                           "normalization_issues": [{"reason": "GOV_NORM_INVENTED",
                                                     "count": 1}],
                           "normalization_issue_records": ["38683"]}),
    ("a zero reason count", {"normalization_issue_count": 1, "normalized_record_count": 232,
                             "normalization_issues": [{"reason": CONTRADICTION, "count": 0}],
                             "normalization_issue_records": ["38683"]}),
    ("a boolean reason count", {"normalization_issue_count": 1, "normalized_record_count": 232,
                                "normalization_issues": [{"reason": CONTRADICTION,
                                                          "count": True}],
                                "normalization_issue_records": ["38683"]}),
    ("a repeated reason", {"normalization_issue_count": 2, "normalized_record_count": 231,
                           "normalization_issues": [{"reason": CONTRADICTION, "count": 1},
                                                    {"reason": CONTRADICTION, "count": 1}],
                           "normalization_issue_records": ["38683", "38684"]}),
    ("reason totals that disagree with the issue count",
     {"normalization_issue_count": 2, "normalized_record_count": 231,
      "normalization_issues": [{"reason": CONTRADICTION, "count": 1}],
      "normalization_issue_records": ["38683", "38684"]}),
    ("issues stated with a zero issue count",
     {"normalization_issues": [{"reason": CONTRADICTION, "count": 1}]}),
    ("issue records stated with a zero issue count",
     {"normalization_issue_records": ["38683"]}),
    ("an issue-record list that is not a list", {"normalization_issue_records": "38683"}),
    ("a non-string issue record", {"normalization_issue_count": 1,
                                   "normalized_record_count": 232,
                                   "normalization_issues": [{"reason": CONTRADICTION,
                                                             "count": 1}],
                                   "normalization_issue_records": [38683]}),
    ("more issue records than the durable bound",
     {"normalization_issue_count": 40, "normalized_record_count": 193,
      "normalization_issues": [{"reason": CONTRADICTION, "count": 40}],
      "normalization_issue_records": [str(900000 + index) for index in range(11)]}),
    ("fewer issue records than the issues name",
     {"normalization_issue_count": 3, "normalized_record_count": 230,
      "normalization_issues": [{"reason": CONTRADICTION, "count": 3}],
      "normalization_issue_records": ["38683"]}),
)


@pytest.mark.parametrize("label,edit", MALFORMED_NORMALIZATION_STATES,
                         ids=[case[0] for case in MALFORMED_NORMALIZATION_STATES])
@pytest.mark.parametrize("acknowledged", [False, True], ids=["plain", "allow_incomplete"])
def test_malformed_durable_normalization_state_is_refused(repository, label, edit,
                                                          acknowledged):
    """And `allow_incomplete` never reaches it.

    An acknowledgement is for a REAL, consistently recorded gap. Malformed or
    self-contradicting state is not a gap a caller can acknowledge, because
    nothing about it can be relied on -- including the count it would be
    acknowledging.
    """
    ingested_then_metadata(repository, **edit)
    view = GovernmentCatalogProjection(repository, allow_incomplete=acknowledged)
    with pytest.raises(GovernmentProjectionError) as failure:
        view.dataset_metadata()
    assert failure.value.reason_code == "GOV_PROJECTION_SNAPSHOT_STATE_INVALID"


def test_a_snapshot_whose_candidates_went_missing_is_refused(repository):
    """Clean-looking metadata, and nothing behind it.

    The summary says 233 rows were read. If the candidates are not there, the
    snapshot is not what it says it is, and answering an empty tree from it
    would be the silent incompleteness the summary exists to prevent.
    """
    lease = leased_run(repository)
    report = ingest(repository, lease)
    repository.catalog_candidates.clear()
    assert stored_metadata(repository, report.snapshot_key)["normalized_record_count"] == \
        PINNED_TOTAL
    for acknowledged in (False, True):
        with pytest.raises(GovernmentProjectionError) as failure:
            GovernmentCatalogProjection(repository,
                                        allow_incomplete=acknowledged).dataset_metadata()
        assert failure.value.reason_code == "GOV_PROJECTION_SNAPSHOT_STATE_INVALID"


def test_two_candidates_for_one_raw_record_are_refused(repository):
    """The count can be right while the SET is wrong.

    Dropping one candidate and duplicating another keeps the total at 233, so
    only a distinctness check catches it: one raw record must be read exactly
    once, or a row is being counted for a vehicle it never described.
    """
    lease = leased_run(repository)
    ingest(repository, lease)
    keys = sorted(repository.catalog_candidates)
    survivor = dict(repository.catalog_candidates[keys[0]])
    dropped = keys[-1]
    repository.catalog_candidates[dropped] = {
        **survivor, "id": str(uuid4()),
        "candidate_key": repository.catalog_candidates[dropped]["candidate_key"]}
    assert len(repository.catalog_candidates) == PINNED_TOTAL
    with pytest.raises(GovernmentProjectionError) as failure:
        GovernmentCatalogProjection(repository).dataset_metadata()
    assert failure.value.reason_code == "GOV_PROJECTION_SNAPSHOT_STATE_INVALID"


def test_a_raw_only_snapshot_holding_a_candidate_is_refused(repository):
    """A resource that states no identities cannot have read any."""
    lease = leased_run(repository)
    ingestor = GovernmentCatalogIngestor(repository, lease,
                                         client=client(transport=quantity_transport()))
    report = ingestor.ingest_resource(src.QUANTITY_RESOURCE_ID, query=dict(PINNED_QUERY))
    snapshot = repository.catalog_snapshots[report.snapshot_key]
    record = next(row for row in repository.catalog_raw_records.values()
                  if row["snapshot_id"] == snapshot["id"])
    repository.catalog_candidates[(snapshot["id"], "cc1." + "e" * 32)] = {
        "id": str(uuid4()), "snapshot_id": snapshot["id"], "raw_record_id": record["id"],
        "candidate_key": "cc1." + "e" * 32, "status": "candidate",
        "manufacturer": "x", "commercial_model": "y", "model_year_start": 2021,
        "model_year_end": 2021, "official_model_code": None, "trim": None,
        "identity_dimensions": {}, "created_at": "2026-09-15T00:00:00+00:00"}
    for acknowledged in (False, True):
        with pytest.raises(GovernmentProjectionError) as failure:
            GovernmentCatalogProjection(repository, resource_id=src.QUANTITY_RESOURCE_ID,
                                        allow_incomplete=acknowledged).dataset_metadata()
        assert failure.value.reason_code in ("GOV_PROJECTION_RESOURCE_NOT_NORMALIZED",
                                             "GOV_PROJECTION_SNAPSHOT_STATE_INVALID")


def test_a_consistently_recorded_gap_is_still_acknowledgeable(repository):
    """The correction must not make a REAL gap unreadable."""
    lease = leased_run(repository)
    bodies, broken = contradictory_capture_bodies()
    report = ingest(repository, lease, transport=FixtureTransport(bodies=bodies))
    view = GovernmentCatalogProjection(repository, snapshot_key=report.snapshot_key,
                                       allow_incomplete=True)
    provenance = view.dataset_metadata()
    assert provenance.normalization_issue_count == 1
    assert provenance.normalization_issues == {CONTRADICTION: 1}
    assert provenance.normalization_issue_records == tuple(broken)
    assert view.list_variants("טויוטה", "RAV4", limit=200).total >= 1


def test_a_projection_refusal_is_never_a_raw_python_error(repository):
    """Every refusal on this path is a static, code-owned reason.

    A `KeyError` or a `ValueError` escaping here would carry a field name, a
    row or a traceback into a caller that is meant to receive a classification.
    """
    for edit in ({"normalized_record_count": None}, {"normalization_issues": None},
                 {"normalization_issue_records": {}}):
        repo = MemoryRepository()
        ingested_then_metadata(repo, **edit)
        with pytest.raises(GovernmentProjectionError) as failure:
            GovernmentCatalogProjection(repo).dataset_metadata()
        assert failure.value.reason_code in projection.GOVERNMENT_PROJECTION_REASONS
        assert failure.value.safe_message


# --- 3. the security documentation states the real boundary ------------------

def test_the_documented_query_boundary_matches_the_code():
    """The package documented "no caller-controlled query parameter". It does
    accept caller-selected `q` and `filters` -- bounded, closed-key and echoed
    back, but caller-selected. The claim is now the accurate one, and this test
    pins the two together so the overclaim cannot come back.
    """
    for path in (GOVERNMENT_PACKAGE / "source.py", GOVERNMENT_PACKAGE / "client.py",
                 Path("docs/catalog-pr2-government-ingestion.md")):
        text = path.read_text(encoding="utf-8")
        assert "no caller-controlled query parameter" not in text, path
        assert "hostname, path or query parameter" not in text, path
    boundary = (GOVERNMENT_PACKAGE / "source.py").read_text(encoding="utf-8")
    assert "Two query parameters ARE caller-selectable" in boundary
    assert "Paging -- `limit` and `offset` -- stays server-owned" in boundary

    # And the code is what the claim describes: exactly two selectable keys,
    # paging refused, and every value sent as a PARAMETER rather than spliced
    # into a URL.
    for allowed in ({"q": "RAV4"}, {"filters": '{"tozar":"x"}'}, {}):
        client()._validated_query(allowed)
    for refused in ({"limit": "10"}, {"offset": "5"}, {"sort": "_id"}, {"resource_id": "x"}):
        refuses("GOV_QUERY_ECHO_MISMATCH", client()._validated_query, refused)

    transport = FixtureTransport()
    client(transport=transport).capture_resource(src.WLTP_RESOURCE_ID,
                                                 query=dict(PINNED_QUERY))
    for action, params in transport.calls:
        assert "?" not in action and "&" not in action
        assert set(params) <= {"id", "resource_id", "limit", "offset", "q", "filters"}
    # Paging is the client's own, on every page it requested.
    offsets = [params["offset"] for action, params in transport.calls
               if action == src.DATASTORE_SEARCH]
    assert offsets == ["0", "100", "200"]


def test_no_sql_is_built_or_executed_anywhere_on_this_path():
    """CKAN's SQL action is not on the allowlist, and no driver is reachable.

    `datastore_search_sql` is the CKAN endpoint that would take a query string;
    it is absent from the package entirely, so there is no SQL for a caller's
    `q` or `filters` to reach. Nor can this package execute SQL of its own: it
    holds no database driver and no cursor.
    """
    assert src.ALLOWED_ACTIONS == {src.PACKAGE_SHOW, src.DATASTORE_SEARCH}
    for path in sorted(GOVERNMENT_PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in ("datastore_search_sql", "psycopg", "sqlalchemy", "sqlite3",
                      ".execute(", "cursor(", "text(\"select", "raw_sql"):
            assert token not in text, f"{path} names {token!r}"

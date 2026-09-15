"""`DataGovClient` -- one bounded, read-only CKAN reader, and nothing else.

Scope
-----

Two CKAN actions, both reads: `package_show` for the dataset's own identity and
version, and `datastore_search` for the rows. No write, no credential, no CKAN
query language, no SQL, and no provider or model call anywhere on this path.

What a caller may and may not choose, exactly:

*   **NOT caller-controlled**: the URL, the scheme, the host, the path, the
    action, the package and the resource. Each comes from a closed allowlist
    checked before the transport is invoked, and `source.action_url` builds the
    one URL that can result.
*   **NOT caller-controlled**: `limit` and `offset`. Paging is this client's
    business, because the completeness gate is arithmetic over the offsets it
    chose.
*   **Caller-selectable, bounded**: `q` and `filters`, and nothing else --
    `_validated_query` refuses any other key, bounds the value and requires
    every page to echo it back.

Every query value is handed to the transport as a PARAMETER and encoded by the
HTTP client. Nothing here concatenates a value into a URL, and nothing on this
path builds SQL at all.

A capture is COMPLETE or it is a refusal
----------------------------------------

The lesson the R5 Government round paid for is that an incomplete capture is
invisible from inside any one page: every page of a CKAN query honestly reports
the full total, so 200 rows of a 233-row query look exactly like a complete
query whose rows happen to number 200. Completeness is therefore a property of
the whole result set and is checked as one, BEFORE a single row is offered to a
caller:

*   the pages are requested at offsets this client CHOSE, in order, at one
    fixed page size, so every boundary is predictable before the first request;
*   each page must echo the resource, the page size, the offset and the query
    it was asked for, must report the SAME total as every other page, must
    report that total as exact rather than estimated, and must hold exactly the
    number of rows its position in the query implies;
*   every page must declare the same field schema;
*   every row must be an object carrying an integer `_id`, and no `_id` may
    appear twice in the capture;
*   the page lengths must SUM to the reported total, and the distinct `_id`
    count must equal it too.

Any of those failing ends the capture with a static reason code. A partial,
inconsistent, over-limit or malformed capture is never returned as material.

What may be retried, and what may not
-------------------------------------

`_request` retries a NETWORK failure, an HTTP 429 and a transient 5xx, a fixed
number of times with fixed backoff, and nothing else. That is structural rather
than a matter of discipline: `_request` returns only after the status, the host,
the media type, the size and the CKAN `success` envelope have passed, and every
schema, identity, pagination and validation rule is applied by its CALLER,
outside the retry loop. A deterministic refusal therefore cannot be retried even
by mistake.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

from backend.catalog.contracts import MAX_RAW_PAYLOAD_CHARS
from backend.engines.swarm_v2.evidence_bounds import SOURCE_VERSION_PATTERNS
from backend.runtime import CancellationRequested

from . import source as src
from .source import GovernmentSourceError
from .transport import DataGovTransport, HttpResponse, TransportFailure

#: How a resource's own metadata is read as a SOURCE VERSION, in preference
#: order, each with the evidence-contract version kind it becomes. Derived from
#: the resource, never invented: if the resource states neither, the capture is
#: refused rather than pinned to something this client made up.
VERSION_FIELDS: tuple[tuple[str, str], ...] = (
    ("revision_id", "document_revision"),
    ("last_modified", "dataset_version"),
)

#: The domain-separated prefix of the field-schema fingerprint, and its version.
SCHEMA_FINGERPRINT_PREFIX = "gov.schema.1"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _as_int(value: Any) -> int | None:
    """An integer the source states as a number or as a digit string.

    CKAN echoes `limit` and `offset` as JSON numbers while a captured query
    records them as the parameter strings that were sent. Both are read; a
    float, a bool or any other text is not an integer here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class ResourceMetadata:
    """One resource's identity and version, read from `package_show`."""

    package_id: str
    resource_id: str
    publisher: str
    dataset_title: str
    upstream_version: str
    upstream_version_kind: str
    retrieved_at: str
    #: The response digest of the metadata call itself -- provenance for the
    #: version above, kept so a reviewer can re-derive where it came from.
    metadata_response_sha256: str
    #: The resource's own published `hash`, when it states one. Recorded as
    #: provenance and deliberately NOT used as the version: for this dataset it
    #: is an MD5 of the full CSV export, which is neither a SHA-256 nor the
    #: JSON the datastore API serves.
    resource_content_hash: str | None = None
    resource_metadata_modified: str | None = None


@dataclass(frozen=True)
class CapturedPage:
    """One response page, with the exact bytes it arrived as."""

    page_number: int
    offset: int
    limit: int
    record_count: int
    reported_total: int
    #: The CAPTURED-RESPONSE CHECKSUM: SHA-256 of this page's response body,
    #: byte for byte. Not the snapshot identity and not a stored-payload
    #: digest -- see `snapshot.py`, which defines all three in one place.
    body_sha256: str
    byte_count: int
    records: tuple[Mapping[str, Any], ...]
    requested_url: str
    final_url: str
    retrieved_at: str
    schema_fingerprint: str


@dataclass(frozen=True)
class ResourceCapture:
    """One COMPLETE, validated query over one allowlisted resource."""

    metadata: ResourceMetadata
    #: The query parameters other than paging, exactly as sent. Empty for an
    #: unfiltered whole-resource capture.
    query: Mapping[str, str]
    page_limit: int
    reported_total: int
    pages: tuple[CapturedPage, ...]
    schema_fingerprint: str
    field_schema: tuple[tuple[str, str], ...]
    started_at: str
    completed_at: str

    @property
    def resource_id(self) -> str:
        return self.metadata.resource_id

    @property
    def record_count(self) -> int:
        return sum(page.record_count for page in self.pages)

    def located_records(self) -> Iterator[tuple[dict[str, Any], Mapping[str, Any]]]:
        """Every captured row with the EXACT position it occupied, in order.

        The locator is the row's place in this capture: which page, at what
        offset, at what index inside that page, and its ordinal across the whole
        capture. Deterministic and reconstructible, so a stored record can be
        pointed back at the response it came out of.
        """
        capture_index = 0
        for page in self.pages:
            for page_index, record in enumerate(page.records):
                yield ({"page_number": page.page_number, "page_offset": page.offset,
                        "page_index": page_index, "capture_index": capture_index},
                       record)
                capture_index += 1


class DataGovClient:
    """A bounded reader for two allowlisted `data.gov.il` resources."""

    def __init__(self, transport: DataGovTransport, *,
                 page_limit: int = src.DEFAULT_PAGE_LIMIT,
                 max_pages: int = src.MAX_PAGES_PER_CAPTURE,
                 max_records: int = src.MAX_RECORDS_PER_CAPTURE,
                 max_response_bytes: int = src.MAX_RESPONSE_BYTES,
                 max_record_chars: int = MAX_RAW_PAYLOAD_CHARS,
                 connect_timeout: float = src.CONNECT_TIMEOUT_SECONDS,
                 read_timeout: float = src.READ_TIMEOUT_SECONDS,
                 max_attempts: int = src.MAX_ATTEMPTS_PER_REQUEST,
                 backoff_seconds: Sequence[float] = src.RETRY_BACKOFF_SECONDS,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 cancellation_checker: Callable[[], bool] | None = None,
                 clock: Callable[[], str] = _utc_now) -> None:
        if not 1 <= int(page_limit) <= src.MAX_PAGE_LIMIT:
            raise GovernmentSourceError("GOV_PAGE_LIMIT_UNEXPECTED")
        if int(max_attempts) < 1 or int(max_pages) < 1 or int(max_records) < 0:
            raise ValueError("government client bounds must be positive")
        self._transport = transport
        self.page_limit = int(page_limit)
        self.max_pages = int(max_pages)
        self.max_records = int(max_records)
        self.max_response_bytes = int(max_response_bytes)
        self.max_record_chars = int(max_record_chars)
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.max_attempts = int(max_attempts)
        self._backoff = tuple(float(value) for value in backoff_seconds)
        self._sleep = sleep_fn
        self._cancellation_checker = cancellation_checker
        self._clock = clock
        #: Every attempt this client made, as `(action, attempt, reason)`.
        #: Bounded observability for tests and telemetry; carries no URL, no
        #: body and no row.
        self.attempts: list[tuple[str, int, str | None]] = []

    # --- the two allowlisted actions -----------------------------------------

    def package_show(self, resource_id: str, *,
                     package_id: str = src.CKAN_PACKAGE_ID) -> ResourceMetadata:
        """The pinned resource's identity and version, or fail closed.

        Both identities are allowlisted BEFORE the request is built, so a
        package or resource this code may not read is never asked for. The
        response is then held to the same package id independently -- two
        checks of one fact, neither standing in for the other.
        """
        package = src.require_allowed_package(package_id)
        identifier = src.require_allowed_resource(resource_id)
        retrieved_at = self._clock()
        document, response, _ = self._request(src.PACKAGE_SHOW, {"id": package})
        result = document["result"]
        if _text(result.get("name")) != package:
            raise GovernmentSourceError("GOV_PACKAGE_IDENTITY_MISMATCH")
        organization = result.get("organization")
        if not isinstance(organization, Mapping) or \
                _text(organization.get("name")) != src.GOVERNMENT_PUBLISHER:
            raise GovernmentSourceError("GOV_PUBLISHER_MISMATCH")
        resources = result.get("resources")
        if not isinstance(resources, list):
            raise GovernmentSourceError("GOV_RESULT_SHAPE_INVALID")
        resource = next((item for item in resources
                         if isinstance(item, Mapping) and _text(item.get("id")) == identifier), None)
        if resource is None:
            raise GovernmentSourceError("GOV_RESOURCE_MISSING")
        version, kind = self._resource_version(resource)
        return ResourceMetadata(
            package_id=package, resource_id=identifier,
            publisher=src.GOVERNMENT_PUBLISHER,
            dataset_title=str(_text(result.get("title")) or package_id),
            upstream_version=version, upstream_version_kind=kind,
            retrieved_at=retrieved_at,
            metadata_response_sha256=hashlib.sha256(response.body).hexdigest(),
            resource_content_hash=_text(resource.get("hash")),
            resource_metadata_modified=_text(resource.get("metadata_modified")))

    @staticmethod
    def _resource_version(resource: Mapping[str, Any]) -> tuple[str, str]:
        """The version the RESOURCE states, in preference order, or refuse.

        Nothing is synthesised here. If the resource publishes neither a
        revision nor a modification time, this capture has no immutable version
        to pin evidence to and is refused -- rather than being pinned to, say,
        the digest of whatever came back, which would make every retrieval look
        like a new upstream version.
        """
        for field, kind in VERSION_FIELDS:
            value = _text(resource.get(field))
            if value is not None and re.fullmatch(SOURCE_VERSION_PATTERNS[kind], value):
                return value, kind
        raise GovernmentSourceError("GOV_RESOURCE_UNVERSIONED")

    def capture_resource(self, resource_id: str, *, package_id: str = src.CKAN_PACKAGE_ID,
                         query: Mapping[str, str] | None = None) -> ResourceCapture:
        """Read ONE complete bounded query over one allowlisted resource.

        `query` holds the non-paging parameters -- `q` and/or `filters` -- and
        is echoed back by every page or the capture fails. Omitting it captures
        the WHOLE resource, page by page, under exactly the same bounds.

        THE METADATA IS ALWAYS READ HERE, through `package_show`. There is no
        override, deliberately: `ResourceMetadata` carries the publisher, the
        source version and the digest of the metadata response -- the three
        things `package_show` validates -- so accepting one from a caller would
        let all three be invented while an allowlist check on the package and
        the resource still passed, and every page, every raw record and the
        snapshot itself would be pinned to provenance no response ever stated.
        `ResourceMetadata` is a RESULT of this path and never an input to it.
        """
        package = src.require_allowed_package(package_id)
        identifier = src.require_allowed_resource(resource_id)
        selection = self._validated_query(query)
        started_at = self._clock()
        metadata = self.package_show(identifier, package_id=package)

        pages: list[CapturedPage] = []
        seen_ids: set[int] = set()
        reported_total: int | None = None
        fingerprint: str | None = None
        field_schema: tuple[tuple[str, str], ...] = ()
        offset = 0
        while True:
            self._check_cancelled()
            if len(pages) >= self.max_pages:
                raise GovernmentSourceError("GOV_PAGE_BUDGET_EXCEEDED")
            page, schema = self._fetch_page(identifier, selection, offset, len(pages) + 1,
                                            reported_total)
            if reported_total is None:
                reported_total = page.reported_total
                if reported_total > self.max_records:
                    raise GovernmentSourceError("GOV_RECORD_BUDGET_EXCEEDED")
                expected_pages = max(1, -(-reported_total // self.page_limit))
                if expected_pages > self.max_pages:
                    raise GovernmentSourceError("GOV_PAGE_BUDGET_EXCEEDED")
            if fingerprint is None:
                fingerprint, field_schema = schema
            elif fingerprint != schema[0]:
                raise GovernmentSourceError("GOV_SCHEMA_DRIFT")
            self._collect_ids(page, seen_ids)
            pages.append(page)
            offset += self.page_limit
            if offset >= reported_total:
                break

        assert reported_total is not None and fingerprint is not None  # loop runs once
        captured = sum(page.record_count for page in pages)
        if captured != reported_total or len(seen_ids) != reported_total:
            # Every page agreed on the total and every page was the size its
            # position implies, yet the pages do not add up to it: the result
            # set is not the query it claims to be.
            raise GovernmentSourceError("GOV_PAGINATION_INCOMPLETE")
        return ResourceCapture(metadata=metadata, query=selection, page_limit=self.page_limit,
                               reported_total=reported_total, pages=tuple(pages),
                               schema_fingerprint=fingerprint, field_schema=field_schema,
                               started_at=started_at, completed_at=self._clock())

    # --- one page ------------------------------------------------------------

    def _fetch_page(self, resource_id: str, selection: Mapping[str, str], offset: int,
                    page_number: int, reported_total: int | None
                    ) -> tuple[CapturedPage, tuple[str, tuple[tuple[str, str], ...]]]:
        params = {"resource_id": resource_id, "limit": str(self.page_limit),
                  "offset": str(offset), **selection}
        retrieved_at = self._clock()
        document, response, requested_url = self._request(src.DATASTORE_SEARCH, params)
        result = document["result"]

        # Identity and echo. A page that answers for another resource, at
        # another offset, at another page size or for another query is not a
        # page of THIS capture, whatever else it holds.
        if _text(result.get("resource_id")) != resource_id:
            raise GovernmentSourceError("GOV_RESOURCE_ECHO_MISMATCH")
        if _as_int(result.get("limit")) != self.page_limit:
            raise GovernmentSourceError("GOV_PAGE_LIMIT_UNEXPECTED")
        if _as_int(result.get("offset")) != offset:
            raise GovernmentSourceError("GOV_PAGE_OFFSET_UNEXPECTED")
        self._check_query_echo(result, selection)

        total = _as_int(result.get("total"))
        if total is None or total < 0:
            raise GovernmentSourceError("GOV_TOTAL_INVALID")
        self._check_total_is_exact(result)
        if reported_total is not None and total != reported_total:
            raise GovernmentSourceError("GOV_PAGE_TOTAL_INCONSISTENT")
        if "records_format" in result and _text(result.get("records_format")) != "objects":
            raise GovernmentSourceError("GOV_RECORDS_FORMAT_UNEXPECTED")

        records = result.get("records")
        if not isinstance(records, list):
            raise GovernmentSourceError("GOV_RESULT_SHAPE_INVALID")
        # The page length its POSITION implies -- computed from the total and
        # the offset, never accepted from what came back.
        expected = max(0, min(self.page_limit, total - offset))
        if len(records) != expected:
            raise GovernmentSourceError("GOV_PAGE_COUNT_UNEXPECTED")
        for record in records:
            if not isinstance(record, Mapping):
                raise GovernmentSourceError("GOV_RECORD_SHAPE_INVALID")
            if len(json.dumps(record, ensure_ascii=False, separators=(",", ":"))) > self.max_record_chars:
                # Refused HERE, before persistence, so an over-large row can
                # never reach a durable write and be rejected halfway through.
                raise GovernmentSourceError("GOV_PAYLOAD_TOO_LARGE")

        page = CapturedPage(
            page_number=page_number, offset=offset, limit=self.page_limit,
            record_count=len(records), reported_total=total,
            body_sha256=hashlib.sha256(response.body).hexdigest(),
            byte_count=len(response.body), records=tuple(records),
            requested_url=requested_url, final_url=str(response.final_url),
            retrieved_at=retrieved_at, schema_fingerprint=schema_fingerprint(result))
        return page, (page.schema_fingerprint, field_schema_of(result))

    @staticmethod
    def _check_total_is_exact(result: Mapping[str, Any]) -> None:
        """`total_was_estimated`, by TYPE and by value.

        An estimated total cannot gate completeness: page lengths that sum to
        an estimate prove nothing about the query. So the flag is part of the
        response contract and is read strictly:

        *   **absent** -- ACCEPTED. CKAN omits the key on responses that did
            not estimate, so refusing an absent key would refuse the ordinary
            case. Absence means the server made no estimation claim, and the
            completeness gate then rests on the total alone.
        *   **JSON `false`** -- accepted; the server states the total is exact.
        *   **JSON `true`** -- refused as an estimate.
        *   **anything else** -- refused as a MALFORMED response, and refused
            as that rather than as an estimate, because a server that answers
            `1`, `"true"`, `null`, `[]` or `{}` here is not answering this
            contract at all.

        An `is True` check accepted every one of those malformed values in
        silence, which is the defect this exists to close. `isinstance(v, bool)`
        is what separates them: in JSON-decoded Python `True`/`False` are the
        only booleans, and `1`/`0` are plain integers.
        """
        if "total_was_estimated" not in result:
            return
        estimated = result["total_was_estimated"]
        if not isinstance(estimated, bool):
            raise GovernmentSourceError("GOV_TOTAL_ESTIMATION_INVALID")
        if estimated:
            raise GovernmentSourceError("GOV_TOTAL_ESTIMATED")

    @staticmethod
    def _check_query_echo(result: Mapping[str, Any], selection: Mapping[str, str]) -> None:
        """The page must answer the query that was sent, and only that one.

        Checked in BOTH directions: a parameter that was sent must come back
        unchanged, and one that was not sent must not come back at all. A page
        that quietly applied a filter nobody asked for is not this query's page.
        """
        for name in ("q", "filters"):
            echoed = result.get(name)
            if name not in selection:
                if _text(echoed) is not None or isinstance(echoed, Mapping) and echoed:
                    raise GovernmentSourceError("GOV_QUERY_ECHO_MISMATCH")
                continue
            expected = selection[name]
            if name == "filters":
                # CKAN echoes `filters` as the decoded object.
                if isinstance(echoed, Mapping):
                    echoed = _canonical_filters(echoed)
                else:
                    echoed = _text(echoed)
            else:
                echoed = _text(echoed)
            if echoed != expected:
                raise GovernmentSourceError("GOV_QUERY_ECHO_MISMATCH")

    def _collect_ids(self, page: CapturedPage, seen: set[int]) -> None:
        """The fail-closed `_id` policy, stated once and applied to every row.

        ONE policy, no variants: every captured row must carry `_id` as a JSON
        integer -- a missing one, a null, a float, a boolean and a digit STRING
        are all refusals -- and an `_id` already seen in this capture is a
        refusal too. A row without a usable register identity cannot be stored
        idempotently or pointed back at the register, and a row reachable twice
        would become two candidates for one vehicle: an ambiguity manufactured
        by the pagination rather than stated by the register.
        """
        for record in page.records:
            identity = record.get("_id")
            if isinstance(identity, bool) or not isinstance(identity, int):
                raise GovernmentSourceError("GOV_RECORD_ID_INVALID")
            if identity in seen:
                raise GovernmentSourceError("GOV_RECORD_ID_DUPLICATED")
            seen.add(identity)
            if len(seen) > self.max_records:
                raise GovernmentSourceError("GOV_RECORD_BUDGET_EXCEEDED")

    @staticmethod
    def _validated_query(query: Mapping[str, str] | None) -> dict[str, str]:
        """The non-paging parameters, bounded and closed.

        Only `q` and `filters` exist here. Paging is this client's business and
        can never be supplied, so a caller cannot ask for an offset or a page
        size that would break the boundary arithmetic the completeness gate
        depends on.
        """
        if not query:
            return {}
        selection: dict[str, str] = {}
        for name, value in query.items():
            if name not in ("q", "filters"):
                raise GovernmentSourceError("GOV_QUERY_ECHO_MISMATCH")
            if isinstance(value, Mapping):
                value = _canonical_filters(value)
            text = _text(value)
            if text is None or len(text) > 200:
                raise GovernmentSourceError("GOV_QUERY_ECHO_MISMATCH")
            selection[name] = text
        return selection

    # --- one request ---------------------------------------------------------

    def _request(self, action: str, params: Mapping[str, str]
                 ) -> tuple[Mapping[str, Any], HttpResponse, str]:
        """One allowlisted request, with the ONLY retry in this package.

        Returns only after the transport, the status, the final host, the media
        type, the response size, the JSON decode and the CKAN `success`
        envelope have all passed. Everything a caller checks afterwards --
        identity, echo, schema, pagination, bounds -- is therefore outside this
        loop and structurally unretryable.
        """
        url = src.action_url(action)
        requested_url = src.canonical_request_url(action, params)
        failure: GovernmentSourceError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._check_cancelled()
            try:
                response = self._transport.get(
                    url, params=dict(params), connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout, max_bytes=self.max_response_bytes)
            except TransportFailure:
                failure = GovernmentSourceError("GOV_TRANSPORT_FAILED", retryable=True)
            else:
                try:
                    document = self._validated_envelope(response)
                except GovernmentSourceError as refusal:
                    failure = refusal
                else:
                    self.attempts.append((action, attempt, None))
                    return document, response, requested_url
            self.attempts.append((action, attempt, failure.reason_code))
            if not failure.retryable or attempt >= self.max_attempts:
                raise failure
            # Finite, fixed backoff. The last configured value repeats if the
            # attempt budget ever exceeds the backoff table.
            self._sleep(self._backoff[min(attempt, len(self._backoff)) - 1]
                        if self._backoff else 0.0)
        raise failure if failure is not None else GovernmentSourceError("GOV_TRANSPORT_FAILED")

    def _validated_envelope(self, response: HttpResponse) -> Mapping[str, Any]:
        """Everything that must hold before a body is worth parsing."""
        if response.truncated or len(response.body) > self.max_response_bytes:
            raise GovernmentSourceError("GOV_RESPONSE_TOO_LARGE")
        if response.redirect_chain or not src.is_approved_url(response.final_url):
            # This client never follows a redirect; a response that arrived
            # from anywhere else is refused rather than read.
            raise GovernmentSourceError("GOV_REDIRECTED_OFF_HOST")
        if int(response.status) != 200:
            raise GovernmentSourceError(
                "GOV_HTTP_STATUS_UNEXPECTED",
                retryable=int(response.status) in src.RETRYABLE_STATUS_CODES)
        media_type = str(response.content_type).split(";", 1)[0].strip().lower()
        if media_type not in src.JSON_CONTENT_TYPES:
            raise GovernmentSourceError("GOV_RESPONSE_NOT_JSON")
        try:
            document = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GovernmentSourceError("GOV_RESPONSE_NOT_JSON") from None
        if not isinstance(document, Mapping):
            raise GovernmentSourceError("GOV_RESPONSE_NOT_JSON")
        if document.get("success") is not True:
            raise GovernmentSourceError("GOV_ENVELOPE_UNSUCCESSFUL")
        if not isinstance(document.get("result"), Mapping):
            raise GovernmentSourceError("GOV_RESULT_SHAPE_INVALID")
        return document

    def _check_cancelled(self) -> None:
        if self._cancellation_checker is not None and self._cancellation_checker():
            raise CancellationRequested("RUN_CANCELLED")


def _canonical_filters(filters: Mapping[str, Any]) -> str:
    """One deterministic rendering of a `filters` object, for send and echo."""
    return json.dumps({str(key): filters[key] for key in sorted(filters)},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def field_schema_of(result: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The resource's declared `(field id, field type)` list, in source order.

    Order is part of what the resource declares -- it is the column order -- so
    it is preserved rather than sorted away. A `fields` list that is absent,
    empty or not made of `{id, type}` objects is a refusal: a page that
    declares no schema cannot be compared with the next one.
    """
    fields = result.get("fields")
    if not isinstance(fields, list) or not fields:
        raise GovernmentSourceError("GOV_SCHEMA_INVALID")
    schema: list[tuple[str, str]] = []
    for field in fields:
        if not isinstance(field, Mapping):
            raise GovernmentSourceError("GOV_SCHEMA_INVALID")
        identifier, kind = _text(field.get("id")), _text(field.get("type"))
        if identifier is None or kind is None:
            raise GovernmentSourceError("GOV_SCHEMA_INVALID")
        schema.append((identifier, kind))
    return tuple(schema)


def schema_fingerprint(result: Mapping[str, Any]) -> str:
    """The SCHEMA FINGERPRINT of one page's declared field list.

    Exactly: `gov.schema.1:` followed by the SHA-256, lowercase hex, of the
    compact JSON array `[[field_id, field_type], ...]` in the order the
    resource declares its fields, encoded UTF-8. It is a function of the
    DECLARED SCHEMA alone -- never of the rows, never of the query, never of
    the byte count -- so two captures of one resource at one schema fingerprint
    the same however many rows each returned, and a resource that gained,
    lost, renamed or retyped a column fingerprints differently.
    """
    basis = json.dumps([list(pair) for pair in field_schema_of(result)],
                       separators=(",", ":"), ensure_ascii=False)
    return f"{SCHEMA_FINGERPRINT_PREFIX}:{hashlib.sha256(basis.encode('utf-8')).hexdigest()}"


__all__ = ["SCHEMA_FINGERPRINT_PREFIX", "VERSION_FIELDS", "CapturedPage", "DataGovClient",
           "ResourceCapture", "ResourceMetadata", "field_schema_of", "schema_fingerprint"]

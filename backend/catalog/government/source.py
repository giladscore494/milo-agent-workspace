"""The pinned `data.gov.il` source: what may be reached, and how far.

Every bound a Government capture is subject to is a server-owned constant in
this module. None of them is environment-tunable, none is derived from model
output, and none is reachable from run input -- so "bounded" is a property a
reviewer can check here rather than a claim about a call site.

The URL is BUILT, never supplied
--------------------------------

The URL, the scheme, the host, the path, the action, the package and the
resource are NOT caller-controlled. A call names an ACTION, a PACKAGE and a
RESOURCE, each from a closed allowlist checked BEFORE the transport is invoked,
and `action_url` assembles the one URL that can result, on the one scheme and
the one host below. A response that arrives from any other host -- including
via a redirect -- is refused rather than read.

Two query parameters ARE caller-selectable, and saying otherwise would be an
overclaim: `q` and `filters`. They are bounded and their key vocabulary is
closed (`DataGovClient._validated_query` refuses any other key, bounds the
value, and requires every page to echo it back), but the VALUES may come from a
caller. Paging -- `limit` and `offset` -- stays server-owned, because the
completeness gate is arithmetic over the offsets this client chose.

Every value is handed to the transport as a parameter and encoded by the HTTP
client; `canonical_request_url` percent-encodes for PROVENANCE only and is
never the string the transport is given. Nothing here concatenates a value into
a URL, and nothing on this path builds SQL.

The ordering matters and is not incidental. An identity that is only checked
against the RESPONSE has already been sent, so `require_allowed_package` and
`require_allowed_resource` run first and the echo checks are a second,
independent test of the same facts rather than the only one.

No credential is held, sent or accepted: `data.gov.il` publishes this dataset
for unauthenticated read, MILO never writes to it, and there is no code path
here that could attach an authorization header.
"""

from __future__ import annotations

import math
import os
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

from backend.catalog.contracts import MAX_RETRIEVAL_METADATA_CHARS

#: The catalog source family every snapshot from this package is filed under.
#: `backend.catalog.contracts` pins it to trust state `evidence`.
GOVERNMENT_SOURCE_FAMILY = "government"

#: The one scheme and the one host. HTTPS only, and a bare host comparison --
#: never a suffix match, which would accept `data.gov.il.example.test`.
DATA_GOV_SCHEME = "https"
DATA_GOV_HOST = "data.gov.il"
DATA_GOV_ACTION_ROOT = f"{DATA_GOV_SCHEME}://{DATA_GOV_HOST}/api/3/action"

#: The CKAN actions this package may call, and nothing else. Both are READS.
PACKAGE_SHOW = "package_show"
DATASTORE_SEARCH = "datastore_search"
ALLOWED_ACTIONS: frozenset[str] = frozenset({PACKAGE_SHOW, DATASTORE_SEARCH})

#: The dataset, and the two resources of it this PR is pinned to.
#:
#: `WLTP_RESOURCE_ID` is the manufacturers-and-models WLTP resource: one row
#: per homologated model/variant, which is the identity material the catalog
#: reads. `QUANTITY_RESOURCE_ID` is the per-manufacturer/model/production-year
#: quantity resource named in the same roadmap section. Both are allowlisted
#: so the client can be pointed at either; only the WLTP resource has a
#: reviewed identity normalization in this PR (see `normalize.py`).
CKAN_PACKAGE_ID = "degem-rechev-wltp"
WLTP_RESOURCE_ID = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
QUANTITY_RESOURCE_ID = "5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"
ALLOWED_RESOURCE_IDS: frozenset[str] = frozenset({WLTP_RESOURCE_ID, QUANTITY_RESOURCE_ID})

#: The packages this code may name. Closed, and checked BEFORE egress.
#:
#: The package id is a SOURCE IDENTITY and it travels in the request, so
#: validating it after the response came back would mean this package had
#: already asked `data.gov.il` for a dataset it had decided to refuse. It is
#: allowlisted here exactly like a resource, and `require_allowed_package` is
#: the first statement of every entry point that accepts one.
ALLOWED_PACKAGE_IDS: frozenset[str] = frozenset({CKAN_PACKAGE_ID})

#: The market this dataset is authoritative for. A property of the SOURCE --
#: the publisher is the Israeli Ministry of Transport and the dataset is the
#: register of models approved for the Israeli market -- and deliberately NOT
#: a per-row field: no row states a market, so none is invented on one.
GOVERNMENT_DATASET_MARKET = "IL"

#: The publisher the package metadata must still name. A dataset that changed
#: hands is not the source this PR reviewed.
GOVERNMENT_PUBLISHER = "ministry_of_transport"

#: Page size. CKAN's datastore accepts a caller-chosen `limit`; this is the
#: one this package sends, so every page boundary is predictable before the
#: first request rather than inferred from what came back.
DEFAULT_PAGE_LIMIT = 100

#: The largest page this package will ever request or accept. A server that
#: answers with more rows than were asked for is refused.
MAX_PAGE_LIMIT = 1000

#: How far ONE capture may go. Reached in either dimension, the capture fails
#: closed BEFORE anything is persisted -- a bounded refusal, never a partial
#: snapshot that looks complete.
MAX_PAGES_PER_CAPTURE = 200
MAX_RECORDS_PER_CAPTURE = 120_000

#: The largest single response body this package will read. Applied by the
#: transport while reading, so an unbounded body is abandoned rather than
#: buffered whole and then measured.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Finite, and separate: a host that accepts a connection and then stops
#: sending must not hold a worker forever.
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 30.0

#: One request is attempted at most this many times IN TOTAL, and only for the
#: transient conditions below. Every other failure -- a schema failure, an
#: identity failure, a validation failure -- is deterministic: retrying it
#: would produce the same answer, so it is raised on the first occurrence.
MAX_ATTEMPTS_PER_REQUEST = 3
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({500, 502, 503, 504})
#: Backoff before attempt 2 and attempt 3. Fixed, finite and in this order.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0)

#: P51: data.gov.il's web application firewall rate-limits bursts. From Cloud
#: Run (production, 2026-09-30) ~90 back-to-back requests were answered 200 and
#: the next one 403 with an HTML block page, after which the address stayed
#: blocked for a while. Two answers to that, both in the ONE request path every
#: reader shares (directory, capture, counts):
#:
#: * PACING -- at least this many seconds between the START of one request of a
#:   client and the start of its next (<= 60 requests a minute). Overridable by
#:   MIN_REQUEST_INTERVAL_ENV within [MIN_REQUEST_INTERVAL_FLOOR,
#:   MIN_REQUEST_INTERVAL_CEILING]; anything else keeps the default.
MIN_REQUEST_INTERVAL_SECONDS = 1.0
MIN_REQUEST_INTERVAL_ENV = "MILO_DATA_GOV_MIN_REQUEST_INTERVAL_SECONDS"
MIN_REQUEST_INTERVAL_FLOOR = 0.5
MIN_REQUEST_INTERVAL_CEILING = 30.0
#: * A LONG BACKOFF for the firewall's answer -- HTTP 403 with an HTML body
#:   (`WAF_BLOCK_MEDIA_TYPES`), or HTTP 429 -- one retry per entry, so at most
#:   four sends. A 403 in any other media type (CKAN's own JSON refusal) is
#:   final, as is every other 4xx.
THROTTLE_BACKOFF_SECONDS: tuple[float, ...] = (60.0, 180.0, 300.0)
WAF_BLOCK_MEDIA_TYPES: tuple[str, ...] = ("text/html",)

#: The JSON media types a CKAN action response may carry.
JSON_CONTENT_TYPES: tuple[str, ...] = ("application/json", "text/json")

#: The bound on the retrieval metadata this package stores on a snapshot is
#: `MAX_RETRIEVAL_METADATA_CHARS` in `backend.catalog.contracts` -- imported
#: above rather than restated, and checked before a write is attempted so an
#: over-long metadata object is a local refusal rather than a database error.
#:
#: How many per-page checksums are carried inline in that bound alongside
#: everything else the metadata states -- including a WORST-CASE normalization
#: summary (every refusal reason present and the bounded id list full), which
#: is what fixes this number rather than an optimistic one.
#:
#: It is a ceiling, not the whole rule: `snapshot.py` also measures the finished
#: object and drops the inline list if it still does not fit, so the bound holds
#: even if a future field grows. A capture with more pages than this still
#: commits to every page checksum through `page_chain_sha256`, which is a single
#: 64-character value however many pages there are.
MAX_INLINE_PAGE_CHECKSUMS = 16


#: The closed vocabulary of capture refusals. Each names the PROPERTY that
#: failed and carries no offset, no row, no URL and no body.
GOVERNMENT_SOURCE_REASONS: Mapping[str, str] = {
    # transport and envelope
    "GOV_TRANSPORT_FAILED": "the government endpoint could not be reached",
    "GOV_HTTP_STATUS_UNEXPECTED": "the government endpoint answered with an unexpected status",
    "GOV_RESPONSE_NOT_JSON": "the government endpoint answered with something other than JSON",
    "GOV_RESPONSE_TOO_LARGE": "a government response exceeds the durable response bound",
    "GOV_REDIRECTED_OFF_HOST": "a government response arrived from an unapproved host",
    "GOV_ENVELOPE_UNSUCCESSFUL": "the government endpoint reported the request unsuccessful",
    "GOV_RESULT_SHAPE_INVALID": "a government response result is not the documented shape",
    # identity
    "GOV_ACTION_NOT_ALLOWED": "that government action is not on the allowlist",
    "GOV_RESOURCE_NOT_ALLOWED": "that government resource is not on the allowlist",
    "GOV_PACKAGE_NOT_ALLOWED": "that government package is not on the allowlist",
    "GOV_PACKAGE_IDENTITY_MISMATCH": "the government metadata is not the pinned package",
    "GOV_PUBLISHER_MISMATCH": "the government dataset is not published by the expected ministry",
    "GOV_RESOURCE_MISSING": "the pinned resource is absent from the government metadata",
    "GOV_RESOURCE_UNVERSIONED": "the pinned resource states no version to read evidence at",
    "GOV_RESOURCE_ECHO_MISMATCH": "a government page answers for a different resource",
    "GOV_QUERY_ECHO_MISMATCH": "a government page answers a different query",
    "GOV_SCHEMA_DRIFT": "government pages of one capture declare different field schemas",
    "GOV_SCHEMA_INVALID": "a government page declares no readable field schema",
    # pagination
    "GOV_PAGE_OFFSET_UNEXPECTED": "a government page is not at the offset the capture needs",
    "GOV_PAGE_LIMIT_UNEXPECTED": "a government page was not served at the requested page size",
    "GOV_PAGE_COUNT_UNEXPECTED": "a government page does not hold the rows the capture needs",
    "GOV_PAGE_TOTAL_INCONSISTENT": "government pages report different totals for one query",
    "GOV_PAGINATION_INCOMPLETE": "the captured pages are not the whole query",
    "GOV_PAGE_BUDGET_EXCEEDED": "this capture would exceed the page bound",
    "GOV_RECORD_BUDGET_EXCEEDED": "this capture would exceed the record bound",
    "GOV_RECORD_ID_DUPLICATED": "one register row appears on more than one captured page",
    "GOV_RECORD_ID_INVALID": "a captured row states no usable register identity",
    "GOV_RECORD_SHAPE_INVALID": "a captured row is not an object",
    # capture-level bounds
    "GOV_TOTAL_INVALID": "a government page reports an unusable total",
    "GOV_TOTAL_ESTIMATED": "a government page reports an ESTIMATED total, which cannot gate completeness",
    "GOV_TOTAL_ESTIMATION_INVALID": "a government page states its estimation flag as something other than a JSON boolean",
    "GOV_RECORDS_FORMAT_UNEXPECTED": "a government page was not served as JSON objects",
    "GOV_METADATA_TOO_LARGE": "the retrieval metadata exceeds the durable bound",
    "GOV_PAYLOAD_TOO_LARGE": "a captured row exceeds the durable raw-record bound",
    # scoped catalog PR2: a declared capture scope must describe the query sent
    "GOV_CAPTURE_SCOPE_MISMATCH":
        "a scoped capture's declared scope does not describe the query it sent",
    # PR-D1: the register directory (every tozar and its row count)
    "GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED":
        "the register directory needs more requests than its hard request cap",
    "GOV_DIRECTORY_TIME_BUDGET_EXCEEDED":
        "the register directory took longer than its hard time cap",
    "GOV_DIRECTORY_RESULT_INVALID":
        "a register directory answer is not the shape a count or a tozar scan must have, "
        "or the scan and the counts disagree",
    "GOV_DIRECTORY_REGISTER_CHANGED":
        "the register's row total changed while the directory was being read",
    # PR-SYNC-1: a register sync stops at the firewall's first answer
    "GOV_SYNC_THROTTLED":
        "data.gov.il's firewall answered the register sync (HTTP 403 page or 429); run it again later",
    "GOV_SYNC_DIRECTORY_TOO_LARGE":
        "more tozars changed than one register sync may count; press Refresh directory once",
}


class GovernmentSourceError(ValueError):
    """A refusal carrying ONLY a static, code-owned reason code.

    The rejected URL, the response body and the row that failed never travel
    with the classification, so the safe representation is fit for a durable
    task result, a run event and telemetry alike.

    `detail` is the one structured addition: the numeric HTTP status of a
    ``GOV_HTTP_STATUS_UNEXPECTED`` answer (an integer, never the body, a
    header or the URL), so an unexpected status can be diagnosed from the log.
    """

    def __init__(self, reason_code: str, *, retryable: bool = False,
                 http_status: int | None = None):
        if reason_code not in GOVERNMENT_SOURCE_REASONS:
            raise ValueError("government source reason must come from the static allowlist")
        self.reason_code = reason_code
        self.retryable = retryable
        self.safe_message = GOVERNMENT_SOURCE_REASONS[reason_code]
        self.http_status = (http_status if reason_code == "GOV_HTTP_STATUS_UNEXPECTED"
                            and isinstance(http_status, int) and not isinstance(http_status, bool)
                            and 100 <= http_status <= 599 else None)
        super().__init__(self.safe_message)

    @property
    def detail(self) -> dict[str, int]:
        return {} if self.http_status is None else {"http_status": self.http_status}


def configured_min_request_interval(env: Mapping[str, str] | None = None) -> float:
    """The pacing interval: MIN_REQUEST_INTERVAL_ENV when it is a number inside
    [FLOOR, CEILING], else the code-owned default -- a malformed or out-of-range
    value never loosens it below the floor."""
    source = os.environ if env is None else env
    raw = str(source.get(MIN_REQUEST_INTERVAL_ENV) or "").strip()
    try:
        value = float(raw) if raw else MIN_REQUEST_INTERVAL_SECONDS
    except ValueError:
        return MIN_REQUEST_INTERVAL_SECONDS
    if not math.isfinite(value) or not MIN_REQUEST_INTERVAL_FLOOR <= value <= MIN_REQUEST_INTERVAL_CEILING:
        return MIN_REQUEST_INTERVAL_SECONDS
    return value


def action_url(action: str) -> str:
    """The one URL an allowlisted action can resolve to.

    The action is looked up in a closed set and then CONCATENATED with a
    literal root, so there is no join, no user path segment and no way for a
    scheme, a host or a path outside `/api/3/action` to appear.
    """
    if action not in ALLOWED_ACTIONS:
        raise GovernmentSourceError("GOV_ACTION_NOT_ALLOWED")
    return f"{DATA_GOV_ACTION_ROOT}/{action}"


def require_allowed_resource(resource_id: str) -> str:
    """The resource id, or fail closed. Exact match against the allowlist."""
    identifier = str(resource_id)
    if identifier not in ALLOWED_RESOURCE_IDS:
        raise GovernmentSourceError("GOV_RESOURCE_NOT_ALLOWED")
    return identifier


def require_allowed_package(package_id: Any) -> str:
    """The package id, or fail closed -- BEFORE anything is sent.

    Exact match, so a near miss (`degem-rechev-wltp-copy`, a case change, a
    padded value) is a refusal rather than a request. `None` and a non-string
    are refusals too: they would otherwise be stringified into a query
    parameter.
    """
    if not isinstance(package_id, str) or package_id not in ALLOWED_PACKAGE_IDS:
        raise GovernmentSourceError("GOV_PACKAGE_NOT_ALLOWED")
    return package_id


def is_approved_url(url: str) -> bool:
    """Whether a URL sits on the approved scheme, host and API path.

    Used on the FINAL url a transport reports, so a redirect that left the
    approved host is visible even though this package never follows one.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    return (parts.scheme == DATA_GOV_SCHEME and parts.hostname == DATA_GOV_HOST
            and parts.port is None and parts.path.startswith("/api/3/action/"))


def canonical_request_url(action: str, params: Mapping[str, str]) -> str:
    """The URL a request is RECORDED as, built from the same closed inputs.

    Deterministic: parameters are emitted in sorted key order and percent
    encoded, so the same logical request records the same URL on every machine
    and in every run. This is provenance, not the string the transport is
    handed -- the transport receives the base URL and the parameter mapping
    separately, so nothing here can smuggle a second query string in.
    """
    base = action_url(action)
    query = "&".join(f"{quote(str(key), safe='')}={quote(str(params[key]), safe='')}"
                     for key in sorted(params))
    return f"{base}?{query}" if query else base


__all__ = ["ALLOWED_ACTIONS", "ALLOWED_PACKAGE_IDS", "ALLOWED_RESOURCE_IDS",
           "CKAN_PACKAGE_ID",
           "CONNECT_TIMEOUT_SECONDS", "DATASTORE_SEARCH", "DATA_GOV_ACTION_ROOT",
           "DATA_GOV_HOST", "DATA_GOV_SCHEME", "DEFAULT_PAGE_LIMIT",
           "GOVERNMENT_DATASET_MARKET", "GOVERNMENT_PUBLISHER", "GOVERNMENT_SOURCE_FAMILY",
           "GOVERNMENT_SOURCE_REASONS", "GovernmentSourceError", "JSON_CONTENT_TYPES",
           "MAX_ATTEMPTS_PER_REQUEST", "MAX_INLINE_PAGE_CHECKSUMS", "MAX_PAGES_PER_CAPTURE",
           "MAX_PAGE_LIMIT", "MAX_RECORDS_PER_CAPTURE", "MAX_RESPONSE_BYTES",
           "MAX_RETRIEVAL_METADATA_CHARS", "MIN_REQUEST_INTERVAL_CEILING", "MIN_REQUEST_INTERVAL_ENV",
           "MIN_REQUEST_INTERVAL_FLOOR", "MIN_REQUEST_INTERVAL_SECONDS", "PACKAGE_SHOW", "QUANTITY_RESOURCE_ID",
           "READ_TIMEOUT_SECONDS", "RETRYABLE_STATUS_CODES", "RETRY_BACKOFF_SECONDS", "THROTTLE_BACKOFF_SECONDS",
           "WAF_BLOCK_MEDIA_TYPES", "WLTP_RESOURCE_ID", "action_url", "canonical_request_url",
           "configured_min_request_interval", "is_approved_url", "require_allowed_package",
           "require_allowed_resource"]

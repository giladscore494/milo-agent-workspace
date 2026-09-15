"""The pinned `data.gov.il` source: what may be reached, and how far.

Every bound a Government capture is subject to is a server-owned constant in
this module. None of them is environment-tunable, none is derived from model
output, and none is reachable from run input -- so "bounded" is a property a
reviewer can check here rather than a claim about a call site.

The URL is BUILT, never supplied
--------------------------------

There is no caller-controlled URL and no caller-controlled hostname anywhere
in this package. A call names an ACTION from a closed allowlist and a RESOURCE
from a closed allowlist, and `action_url` assembles the one URL that can
result, on the one scheme and the one host below. A response that arrives from
any other host -- including via a redirect -- is refused rather than read.

No credential is held, sent or accepted: `data.gov.il` publishes this dataset
for unauthenticated read, MILO never writes to it, and there is no code path
here that could attach an authorization header.
"""

from __future__ import annotations

from typing import Mapping
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
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
#: Backoff before attempt 2 and attempt 3. Fixed, finite and in this order.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0)

#: The JSON media types a CKAN action response may carry.
JSON_CONTENT_TYPES: tuple[str, ...] = ("application/json", "text/json")

#: The bound on the retrieval metadata this package stores on a snapshot is
#: `MAX_RETRIEVAL_METADATA_CHARS` in `backend.catalog.contracts` -- imported
#: above rather than restated, and checked before a write is attempted so an
#: over-long metadata object is a local refusal rather than a database error.
#:
#: How many per-page checksums fit inline in that bound alongside everything
#: else the metadata states. A capture with more pages than this still commits
#: to every page checksum through `page_chain_sha256` (see `snapshot.py`),
#: which is a single 64-character value however many pages there are.
MAX_INLINE_PAGE_CHECKSUMS = 24


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
    "GOV_RECORDS_FORMAT_UNEXPECTED": "a government page was not served as JSON objects",
    "GOV_METADATA_TOO_LARGE": "the retrieval metadata exceeds the durable bound",
    "GOV_PAYLOAD_TOO_LARGE": "a captured row exceeds the durable raw-record bound",
}


class GovernmentSourceError(ValueError):
    """A refusal carrying ONLY a static, code-owned reason code.

    The rejected URL, the response body and the row that failed never travel
    with the classification, so the safe representation is fit for a durable
    task result, a run event and telemetry alike.
    """

    def __init__(self, reason_code: str, *, retryable: bool = False):
        if reason_code not in GOVERNMENT_SOURCE_REASONS:
            raise ValueError("government source reason must come from the static allowlist")
        self.reason_code = reason_code
        self.retryable = retryable
        self.safe_message = GOVERNMENT_SOURCE_REASONS[reason_code]
        super().__init__(self.safe_message)


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


__all__ = ["ALLOWED_ACTIONS", "ALLOWED_RESOURCE_IDS", "CKAN_PACKAGE_ID",
           "CONNECT_TIMEOUT_SECONDS", "DATASTORE_SEARCH", "DATA_GOV_ACTION_ROOT",
           "DATA_GOV_HOST", "DATA_GOV_SCHEME", "DEFAULT_PAGE_LIMIT",
           "GOVERNMENT_DATASET_MARKET", "GOVERNMENT_PUBLISHER", "GOVERNMENT_SOURCE_FAMILY",
           "GOVERNMENT_SOURCE_REASONS", "GovernmentSourceError", "JSON_CONTENT_TYPES",
           "MAX_ATTEMPTS_PER_REQUEST", "MAX_INLINE_PAGE_CHECKSUMS", "MAX_PAGES_PER_CAPTURE",
           "MAX_PAGE_LIMIT", "MAX_RECORDS_PER_CAPTURE", "MAX_RESPONSE_BYTES",
           "MAX_RETRIEVAL_METADATA_CHARS", "PACKAGE_SHOW", "QUANTITY_RESOURCE_ID",
           "READ_TIMEOUT_SECONDS", "RETRYABLE_STATUS_CODES", "RETRY_BACKOFF_SECONDS",
           "WLTP_RESOURCE_ID", "action_url", "canonical_request_url", "is_approved_url",
           "require_allowed_resource"]

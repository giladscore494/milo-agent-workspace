"""ONE fixture-backed transport, serving the committed R5 Government capture.

Why the R5 fixtures rather than new ones
----------------------------------------

`backend/testing/r5_proof/fixtures/government/` already holds a REAL bounded
`data.gov.il` capture -- `q=RAV4&limit=100` over the pinned WLTP resource,
offsets 0/100/200, counts 100/100/33, reported total 233 -- committed
byte-for-byte as `exact_response` fixtures with their own checksums and
provenance in the R5 manifest, and independently verified against the signed
capture archive when they were imported.

Catalog PR2 reads exactly those bytes. Nothing is re-captured, nothing is
re-derived, and no second definition of a Government field, page or checksum is
created: this module reads each fixture THROUGH the R5 manifest gate, so every
byte a PR2 test sees has been re-hashed against the digest R5 recorded, and a
single changed byte fails the test closed rather than quietly producing
different material.

What is a FIXTURE and what is PRODUCTION
----------------------------------------

The bytes and the pinned query are fixture. `DataGovClient` is production code
and is generic: it chooses its own offsets from its own page size, computes the
length every page must have from the reported total, and reads any number of
pages under its bounds. The committed capture is small enough to hold in a
repository and large enough to be a real three-page query, which is why the
tests use it -- not because the client is specialised to it.

Test support only: imported by tests, never by a production entrypoint. It
opens no socket; it serves committed bytes.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from backend.catalog.government import source as src
from backend.catalog.government.transport import HttpResponse, TransportFailure
from backend.testing.r5_proof.manifest import verify_fixture

#: The committed capture, exactly as R5 pinned it.
PINNED_QUERY: Mapping[str, str] = {"q": "RAV4"}
PINNED_PAGE_LIMIT = 100
PINNED_TOTAL = 233
PINNED_PAGE_COUNTS = (100, 100, 33)

#: Offset -> the R5 manifest source key of the page captured at that offset.
PAGE_SOURCE_KEYS: Mapping[int, str] = {
    0: "government_wltp_page_1",
    100: "government_wltp_page_2",
    200: "government_wltp_page_3",
}
PACKAGE_SOURCE_KEY = "government_package"

#: The one row R5 selected, kept here so a PR2 test can name the same row.
PINNED_RECORD_ID = 36327

#: The media type the capture recorded.
CAPTURED_CONTENT_TYPE = "application/json;charset=utf-8"


def fixture_body(source_key: str) -> bytes:
    """One committed response body, re-hashed against the R5 manifest first."""
    _, payload = verify_fixture(source_key)
    return payload


def page_body(offset: int) -> bytes:
    return fixture_body(PAGE_SOURCE_KEYS[int(offset)])


def package_body() -> bytes:
    return fixture_body(PACKAGE_SOURCE_KEY)


def page_document(offset: int) -> dict[str, Any]:
    """One committed page, decoded, for a test that needs to MUTATE it.

    A mutated document is re-encoded by `encode`, so what the client sees is a
    body that differs from the committed one in exactly the stated way.
    """
    return json.loads(page_body(offset).decode("utf-8"))


def encode(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


class FixtureTransport:
    """A `DataGovTransport` that answers from the committed capture.

    Every failure mode a test needs is a constructor argument rather than a
    subclass, so a test reads as one statement about one condition:

    *   `bodies` replaces the body served for one offset (or `"package"`),
        which is how a missing, short, reordered, duplicated or inconsistent
        page is expressed;
    *   `statuses` is consumed one entry per request and returned INSTEAD of
        serving the fixture, which is how 429, a transient 5xx and a
        non-transient 4xx are expressed;
    *   `transport_failures` makes the leading N requests raise, which is how a
        network failure is expressed;
    *   `content_type`, `final_url` and `truncated` express a wrong media type,
        a redirect off the approved host and an over-long body.

    Every call is recorded in `calls` as `(action, params)`, so a test can
    assert exactly how many requests were made and at which offsets -- which is
    what makes "the retry budget is finite" and "a deterministic failure is not
    retried" checkable rather than asserted.
    """

    def __init__(self, *, bodies: Mapping[Any, bytes] | None = None,
                 statuses: Sequence[int] = (), transport_failures: int = 0,
                 content_type: str = CAPTURED_CONTENT_TYPE, final_url: str | None = None,
                 truncated: bool = False) -> None:
        self._bodies = dict(bodies or {})
        self._statuses = list(statuses)
        self._transport_failures = int(transport_failures)
        self._content_type = content_type
        self._final_url = final_url
        self._truncated = truncated
        self.calls: list[tuple[str, dict[str, str]]] = []
        #: `(connect_timeout, read_timeout, max_bytes)` as the client passed
        #: them, so a test can assert the bounds are finite and are actually
        #: reaching the transport rather than being defaults it never sees.
        self.bounds: list[tuple[float, float, int]] = []

    def get(self, url: str, *, params: Mapping[str, str], connect_timeout: float,
            read_timeout: float, max_bytes: int) -> HttpResponse:
        action = str(url).rsplit("/", 1)[-1]
        self.calls.append((action, dict(params)))
        self.bounds.append((connect_timeout, read_timeout, max_bytes))
        if self._transport_failures > 0:
            self._transport_failures -= 1
            raise TransportFailure("fixture transport failure")
        final = self._final_url or src.canonical_request_url(action, params)
        if self._statuses:
            return HttpResponse(status=int(self._statuses.pop(0)), body=b"{}",
                                content_type=self._content_type, final_url=final)
        if action == src.PACKAGE_SHOW:
            body = self._bodies.get("package", package_body())
        else:
            offset = int(params["offset"])
            body = self._bodies.get(offset, _default_page_body(offset))
        return HttpResponse(status=200, body=body, content_type=self._content_type,
                            final_url=final, truncated=self._truncated)


def _default_page_body(offset: int) -> bytes:
    """The committed page at that offset, or an EMPTY page beyond the query.

    A capture whose reported total is 233 never asks for offset 300, so a
    request past the end means the client's own boundary arithmetic went wrong.
    Answering it with a well-formed empty page rather than raising makes that a
    visible pagination refusal in the test instead of a KeyError.
    """
    if offset in PAGE_SOURCE_KEYS:
        return page_body(offset)
    document = page_document(0)
    document["result"]["offset"] = offset
    document["result"]["records"] = []
    return encode(document)


__all__ = ["CAPTURED_CONTENT_TYPE", "PACKAGE_SOURCE_KEY", "PAGE_SOURCE_KEYS",
           "PINNED_PAGE_COUNTS", "PINNED_PAGE_LIMIT", "PINNED_QUERY", "PINNED_RECORD_ID",
           "PINNED_TOTAL", "FixtureTransport", "encode", "fixture_body", "package_body",
           "page_body", "page_document"]

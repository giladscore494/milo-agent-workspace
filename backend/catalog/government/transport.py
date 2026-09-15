"""The ONLY module in this package that can open a socket.

Everything else -- the client, the normalizer, the ingestor and the query
projection -- is pure logic over material a transport already returned, so the
whole capture path is testable, and is tested, with no network at all. A test
asserts that no other module here so much as names an HTTP library, which makes
"offline in CI" a property of the code rather than a convention.

What the transport does, and refuses to do
------------------------------------------

*   HTTPS only, to one host, on one API path. The base URL is built by
    `source.action_url` from a closed action allowlist; the caller passes
    PARAMETERS, never a URL, so no hostname, scheme or path can come from a
    caller, a plan or a model.
*   Redirects are NEVER followed. A 3xx is a refusal, because a redirect is
    exactly the mechanism by which an approved host hands a reader to an
    unapproved one.
*   No credential of any kind. There is no parameter, field or environment
    read here that could become an `Authorization` header, a cookie or a
    token, and the request is sent with a bare `Accept` and `User-Agent`.
*   The body is read INCREMENTALLY against a byte bound and abandoned the
    moment it is exceeded, so an unbounded response is never buffered whole
    and then measured.
*   Connect and read timeouts are finite and separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .source import (CONNECT_TIMEOUT_SECONDS, DATA_GOV_HOST, MAX_RESPONSE_BYTES,
                     READ_TIMEOUT_SECONDS)

#: What this package sends about itself. No version of any credential, and no
#: identifying user information.
USER_AGENT = "milo-catalog-government/1 (+read-only public dataset capture)"


class TransportFailure(Exception):
    """A NETWORK-level failure: nothing was received, or the read broke.

    Deliberately distinct from every response-level failure. This is the one
    condition -- alongside HTTP 429 and a transient 5xx -- for which the client
    may retry, because retrying a deterministic refusal would only produce the
    same refusal again.
    """


@dataclass(frozen=True)
class HttpResponse:
    """One complete, bounded HTTP response, as data."""

    status: int
    body: bytes
    content_type: str
    final_url: str
    #: True when the body hit the byte bound and was abandoned. The client
    #: refuses such a response rather than parsing a prefix of it.
    truncated: bool = False
    #: Every hop a transport followed. This transport follows none, so it is
    #: always empty here; it exists so the client can state the fact rather
    #: than assume it.
    redirect_chain: tuple[str, ...] = ()


class DataGovTransport(Protocol):
    """The seam the client is written against.

    `url` is always a value `source.action_url` produced. A transport may
    refuse anything it does not recognise, but it is never the component that
    decides what is allowed -- the client validates the host, the status, the
    media type and the size of whatever comes back.
    """

    def get(self, url: str, *, params: Mapping[str, str], connect_timeout: float,
            read_timeout: float, max_bytes: int) -> HttpResponse: ...


class HttpsDataGovTransport:
    """The real transport. Constructed explicitly; never a default anywhere.

    No production entrypoint builds one in this PR: the ingestion path takes a
    transport as a constructor argument and the worker wires none, so a live
    capture is a deliberate act rather than something a release turns on.
    """

    def __init__(self, *, session: Any = None) -> None:
        if session is None:
            import requests  # imported lazily so importing this module needs no network stack

            session = requests.Session()
            # A Session carries no cookies into this package's requests and is
            # never given any; trust_env is off so no proxy credential, netrc
            # entry or CA override can come from the environment.
            session.trust_env = False
        self._session = session

    def get(self, url: str, *, params: Mapping[str, str], connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
            read_timeout: float = READ_TIMEOUT_SECONDS,
            max_bytes: int = MAX_RESPONSE_BYTES) -> HttpResponse:
        import requests

        try:
            response = self._session.get(
                url, params=dict(params), timeout=(connect_timeout, read_timeout),
                allow_redirects=False, stream=True,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                         "Host": DATA_GOV_HOST})
        except requests.RequestException as failure:
            # `from None`: the library message can quote the URL and the
            # proxy, and a refusal reason must carry neither.
            raise TransportFailure("government transport failed") from None
        try:
            body, truncated = self._read_bounded(response, max_bytes)
            return HttpResponse(status=int(response.status_code), body=body,
                                content_type=str(response.headers.get("Content-Type", "")),
                                final_url=str(response.url), truncated=truncated)
        except requests.RequestException:
            raise TransportFailure("government transport failed") from None
        finally:
            response.close()

    @staticmethod
    def _read_bounded(response: Any, max_bytes: int) -> tuple[bytes, bool]:
        """Read at most `max_bytes` + 1, then stop. The extra byte is the proof."""
        chunks: list[bytes] = []
        seen = 0
        for chunk in response.iter_content(chunk_size=65_536):
            if not chunk:
                continue
            chunks.append(chunk)
            seen += len(chunk)
            if seen > max_bytes:
                return b"".join(chunks)[: max_bytes + 1], True
        return b"".join(chunks), False


__all__ = ["USER_AGENT", "DataGovTransport", "HttpResponse", "HttpsDataGovTransport",
           "TransportFailure"]

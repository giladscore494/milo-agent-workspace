"""P52: the transport sends the crawler token data.gov.il documents for automated clients.

data.gov.il's API examples page (section "צריכת נתונים על ידי crawling") asks
automated clients to add `datagov-external-client` to their User-Agent. The
transport sends it alongside our own identification, and the snapshot's
provenance (`CAPTURE_TOOL`) is untouched, so snapshot identity and hashes do
not move. No socket is opened: the transport is given a fake session.
"""

from __future__ import annotations

from typing import Any

from backend.catalog.government import snapshot, source, transport


class _FakeResponse:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    url = "https://data.gov.il/api/3/action/package_show"

    def iter_content(self, chunk_size: int) -> Any:
        yield b'{"success": true}'

    def close(self) -> None:
        pass


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse()


def test_outgoing_user_agent_carries_crawler_token_and_our_identification() -> None:
    session = _RecordingSession()
    response = transport.HttpsDataGovTransport(session=session).get(
        source.action_url("package_show"), params={"id": "x"})

    assert response.status == 200
    assert len(session.calls) == 1
    user_agent = session.calls[0]["headers"]["User-Agent"]
    assert "datagov-external-client" in user_agent
    assert "milo-catalog-government/1" in user_agent
    assert transport.DATAGOV_CRAWLER_TOKEN == "datagov-external-client"
    assert user_agent == transport.USER_AGENT


def test_user_agent_adds_no_other_header() -> None:
    session = _RecordingSession()
    transport.HttpsDataGovTransport(session=session).get(
        source.action_url("package_show"), params={"id": "x"})

    assert set(session.calls[0]["headers"]) == {"Accept", "User-Agent", "Host"}


def test_capture_tool_provenance_is_unchanged() -> None:
    # Snapshot provenance, part of snapshot identity: never the User-Agent.
    assert snapshot.CAPTURE_TOOL == "milo-catalog-government/1"
    assert "datagov-external-client" not in snapshot.CAPTURE_TOOL

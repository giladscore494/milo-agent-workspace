"""P51: data.gov.il's firewall rate-limits bursts; every reader is paced and waits it out.

Production, from Cloud Run (same image, same egress address): a register
directory refresh sent ~90 requests back to back and got 200 for each, then
request 93 was answered 403 with an HTML block page (not CKAN JSON), and the
address stayed blocked for a while -- single requests with or without the
explicit Host header all answered 200. So, in the ONE request path the
directory, the capture and the counts share (`DataGovClient._request`):

* every send of a client starts at least `MIN_REQUEST_INTERVAL_SECONDS` (1 s)
  after its previous one -- retries included, a retry's own wait counting;
* the firewall's answer -- 403 with an HTML body, or 429 -- is sent again
  after 60 s, 180 s and 300 s, each wait one log line (status, attempt,
  seconds; no body, header or URL); a 403 in JSON (CKAN's own refusal) is
  final, as is every other 4xx;
* the register directory charges every retry to its request cap and refuses a
  wait that would end past its time cap before waiting.

Every test runs on a fake clock: the transport "takes" `LATENCY` seconds per
answer and every wait the client asks for advances the clock.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping

import pytest

from backend.catalog.government import source as src
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.directory import (DEFAULT_MAX_SECONDS, count_tozar, discover_directory)
from backend.catalog.government.source import GovernmentSourceError
from backend.catalog.government.transport import HttpResponse
from backend.testing.government_capture import encode, page_document
from tests.test_register_capture import LEXUS, TOYOTA, DirectoryClient

REPO = Path(__file__).resolve().parents[1]
LATENCY = 0.25
WAF_PAGE = b"<!DOCTYPE HTML PUBLIC \"-//IETF//DTD HTML 2.0//EN\"><html><body>blocked LEAKED-BODY-MARKER</body></html>"
CKAN_403 = b'{"success": false, "error": {"message": "LEAKED-BODY-MARKER"}}'


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(round(seconds, 6))
        self.now += seconds


class RegisterTransport:
    """The register over HTTP (the DirectoryClient register behind the REAL
    client). `refusals` maps a send index (0 = the first send) to
    (status, media type, body) served instead."""

    def __init__(self, clock: Clock, counts: Mapping[str | None, int],
                 refusals: Mapping[int, tuple[int, str, bytes]] | None = None) -> None:
        self.clock = clock
        self.register = DirectoryClient(counts)
        self.refusals = dict(refusals or {})
        self.sent: list[tuple[float, dict[str, str]]] = []

    def get(self, url: str, *, params: Mapping[str, str], connect_timeout: float,
            read_timeout: float, max_bytes: int) -> HttpResponse:
        index = len(self.sent)
        self.sent.append((self.clock.now, dict(params)))
        self.clock.now += LATENCY
        final_url = src.canonical_request_url(src.DATASTORE_SEARCH, params)
        if index in self.refusals:
            status, media_type, body = self.refusals[index]
            return HttpResponse(status=status, body=body, content_type=media_type, final_url=final_url)
        document, _response, _url = self.register._request(src.DATASTORE_SEARCH, params)
        return HttpResponse(status=200, body=json.dumps(document).encode("utf-8"),
                            content_type="application/json; charset=utf-8", final_url=final_url)


def paced_client(transport: Any, clock: Clock, interval: float = 1.0, **kwargs: Any) -> DataGovClient:
    return DataGovClient(transport, min_request_interval=interval, sleep_fn=clock.sleep, monotonic_fn=clock,
                         **kwargs)


def html_403() -> tuple[int, str, bytes]:
    return 403, "text/html; charset=iso-8859-1", WAF_PAGE


def _gaps(transport: RegisterTransport) -> list[float]:
    starts = [at for at, _params in transport.sent]
    return [round(later - earlier, 6) for earlier, later in zip(starts, starts[1:])]


# -- 1. pacing -------------------------------------------------------------------------------

def test_the_interval_is_code_owned_at_one_second_with_a_bounded_override(monkeypatch):
    text = (REPO / "backend" / "catalog" / "government" / "source.py").read_text(encoding="utf-8")
    assert re.search(r"^MIN_REQUEST_INTERVAL_SECONDS = 1\.0$", text, re.M), "the production interval"
    assert re.search(r"^THROTTLE_BACKOFF_SECONDS: tuple\[float, \.\.\.\] = \(60\.0, 180\.0, 300\.0\)$", text, re.M)
    monkeypatch.setattr(src, "MIN_REQUEST_INTERVAL_SECONDS", 1.0)  # conftest zeroes it offline
    env = src.MIN_REQUEST_INTERVAL_ENV
    assert src.configured_min_request_interval({}) == 1.0
    assert src.configured_min_request_interval({env: "2.5"}) == 2.5
    # Never below the floor, never above the ceiling, never from a malformed value.
    for value in ("0", "0.1", "-1", "31", "nan", "inf", "fast"):
        assert src.configured_min_request_interval({env: value}) == 1.0, value
    monkeypatch.setenv(env, "3")
    assert DataGovClient(RegisterTransport(Clock(), {})).min_request_interval == 3.0


def test_every_pair_of_requests_is_paced_directory_and_counts_alike():
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28, LEXUS: 2500, "מאזדה": 700})
    found = discover_directory(paced_client(transport, clock), clock=clock)
    assert len(found.units) == 3
    kinds = ["distinct" if p.get("distinct") else "count" if "filters" in p else "scan" for _t, p in transport.sent]
    assert kinds == ["distinct", "scan", "scan", "scan", "scan", "count", "count", "count"]
    # Each send starts exactly 1 s after the previous one: the answer took
    # LATENCY, the client waited the rest -- between EVERY pair, counts included.
    assert _gaps(transport) == [1.0] * 7
    assert clock.waits == [1.0 - LATENCY] * 7


def test_a_slow_answer_is_not_paced_again():
    class Slow(RegisterTransport):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            self.clock.now += 2.0  # the answer took longer than the interval
            return response
    clock = Clock()
    transport = Slow(clock, {TOYOTA: 5})
    discover_directory(paced_client(transport, clock), clock=clock)
    assert clock.waits == [] and len(transport.sent) == 3


# -- 2. the firewall's answer ------------------------------------------------------------------

def test_an_html_403_then_200_succeeds_after_the_first_backoff(caplog):
    clock = Clock()
    # Send 1 is the first scan page (after the distinct cross-check).
    transport = RegisterTransport(clock, {TOYOTA: 28}, refusals={1: html_403()})
    with caplog.at_level(logging.WARNING, logger="backend.catalog.government.client"):
        found = discover_directory(paced_client(transport, clock), clock=clock)
    assert [(u.tozar, u.expected_rows) for u in found.units] == [(TOYOTA, 28)]
    assert transport.sent[1][1] == transport.sent[2][1], "the refused page was sent again"
    assert found.requests == 4, "distinct + refused scan + its retry + count: the retry is a request"
    # 60 s, and no extra pace after it (the wait counts as time passed).
    assert clock.waits == [1.0 - LATENCY, 60.0, 1.0 - LATENCY]
    assert _gaps(transport)[1] == LATENCY + 60.0
    lines = [record.getMessage() for record in caplog.records]
    assert lines == [
        "GOV_HTTP_STATUS_UNEXPECTED action=datastore_search attempt=1 http_status=403 content_type=text/html "
        "body_html=true body_json=false",
        "GOV_REQUEST_RETRY_WAIT http_status=403 attempt=1 wait_seconds=60"]
    assert "LEAKED" not in caplog.text and "data.gov.il" not in caplog.text and "resource_id" not in caplog.text


def test_four_html_403s_fail_with_the_status_after_the_bounded_waits(caplog):
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28}, refusals={i: html_403() for i in range(1, 5)})
    with caplog.at_level(logging.WARNING, logger="backend.catalog.government.client"):
        with pytest.raises(GovernmentSourceError) as refused:
            discover_directory(paced_client(transport, clock), clock=clock)
    assert refused.value.reason_code == "GOV_HTTP_STATUS_UNEXPECTED"
    assert refused.value.detail == {"http_status": 403}
    assert len(transport.sent) == 5
    assert [w for w in clock.waits if w >= 60] == [60.0, 180.0, 300.0]
    assert [r.getMessage() for r in caplog.records if "RETRY_WAIT" in r.getMessage()] == [
        f"GOV_REQUEST_RETRY_WAIT http_status=403 attempt={n} wait_seconds={w}"
        for n, w in ((1, 60), (2, 180), (3, 300))]
    assert "LEAKED" not in str(refused.value) + repr(vars(refused.value)) + caplog.text


def test_a_json_403_is_final():
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28},
                                  refusals={0: (403, "application/json", CKAN_403)})
    with pytest.raises(GovernmentSourceError) as refused:
        count_tozar(paced_client(transport, clock), TOYOTA, clock=clock)
    assert refused.value.detail == {"http_status": 403}
    assert len(transport.sent) == 1 and clock.waits == []


@pytest.mark.parametrize("media_type,body", [("application/json", CKAN_403), ("text/html", WAF_PAGE)])
def test_a_429_waits_on_the_same_schedule(media_type, body):
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28}, refusals={0: (429, media_type, body), 1: (429, media_type, body)})
    assert count_tozar(paced_client(transport, clock), TOYOTA, clock=clock) == 28
    assert clock.waits == [60.0, 180.0]


@pytest.mark.parametrize("status", [400, 401, 404, 410])
def test_every_other_4xx_is_final_even_as_html(status):
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28}, refusals={0: (status, "text/html", WAF_PAGE)})
    with pytest.raises(GovernmentSourceError) as refused:
        count_tozar(paced_client(transport, clock), TOYOTA, clock=clock)
    assert refused.value.detail == {"http_status": status} and len(transport.sent) == 1


# -- 3. the caps still bound the directory -------------------------------------------------------

def test_the_time_cap_refuses_a_wait_before_it_is_taken():
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28}, refusals={i: html_403() for i in range(1, 5)})
    with pytest.raises(GovernmentSourceError) as late:
        discover_directory(paced_client(transport, clock), max_seconds=400, clock=clock)
    assert late.value.reason_code == "GOV_DIRECTORY_TIME_BUDGET_EXCEEDED"
    # 60 s and 180 s were waited; 300 s more would end past 400 s: never taken.
    assert [w for w in clock.waits if w >= 60] == [60.0, 180.0] and clock.now < 400
    assert len(transport.sent) == 4, "distinct, the refused page and its two retries"


def test_the_request_cap_counts_every_retry():
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28}, refusals={1: html_403()})
    with pytest.raises(GovernmentSourceError) as refused:
        discover_directory(paced_client(transport, clock), max_requests=2, clock=clock)
    assert refused.value.reason_code == "GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED"
    assert len(transport.sent) == 2 and 60.0 not in clock.waits, "refused before the wait and the retry"


def test_the_final_count_of_a_capture_keeps_its_retries():
    clock = Clock()
    transport = RegisterTransport(clock, {TOYOTA: 28}, refusals={0: html_403(), 1: html_403()})
    assert count_tozar(paced_client(transport, clock), TOYOTA, clock=clock) == 28
    assert clock.waits == [60.0, 180.0]


# -- 4. what the pacing costs, measured on the fake clock ----------------------------------------

def _register_like_production() -> dict[str, int]:
    """137 tozars, 101,691 rows: the register's size on 2026-09-30."""
    counts = {f"יצרן-{index:03d}": 700 for index in range(136)}
    counts["יצרן-גדול"] = 101_691 - 136 * 700
    return counts


def test_a_full_directory_takes_about_four_minutes_well_inside_its_cap(capsys):
    clock = Clock()
    transport = RegisterTransport(clock, _register_like_production())
    found = discover_directory(paced_client(transport, clock), clock=clock)
    assert found.total_rows == 101_691 and len(found.units) == 137
    # 1 distinct + 102 scan pages + 137 counts.
    assert found.requests == len(transport.sent) == 240
    assert clock.now == pytest.approx(239 * 1.0 + LATENCY)
    assert clock.now < DEFAULT_MAX_SECONDS / 10, "the 3000 s cap covers it more than ten times over"
    with capsys.disabled():
        print(f"\nP51 directory: {found.requests} requests, {clock.now:.0f} s paced at 1 s "
              f"(answers {LATENCY} s each); cap {DEFAULT_MAX_SECONDS:.0f} s")


def _scoped_pages(rows: int, marque: str, limit: int) -> dict[Any, bytes]:
    template = page_document(0)
    record = dict(template["result"]["records"][0])
    bodies: dict[Any, bytes] = {}
    for offset in range(0, rows, limit):
        document = copy.deepcopy(template)
        result = document["result"]
        result.pop("q", None)
        result.update(filters={"tozar": marque}, limit=limit, offset=offset, total=rows, total_was_estimated=False,
                      records=[dict(record, _id=offset + index + 1, tozar=marque)
                               for index in range(min(limit, rows - offset))])
        bodies[offset] = encode(document)
    count = copy.deepcopy(template)
    count["result"].pop("q", None)
    count["result"].update(filters={"tozar": marque}, limit=0, total=rows, total_was_estimated=False, records=[])
    count["result"].pop("offset", None)
    bodies["count"] = encode(count)
    return bodies


def test_a_10000_row_capture_takes_about_twelve_seconds(capsys):
    from backend.testing.government_capture import FixtureTransport

    clock = Clock()

    class Timed(FixtureTransport):
        def get(self, url, **kwargs):
            clock.now += LATENCY
            return super().get(url, **kwargs)
    transport = Timed(bodies=_scoped_pages(10_000, TOYOTA, src.MAX_PAGE_LIMIT))
    client = paced_client(transport, clock, page_limit=src.MAX_PAGE_LIMIT)
    capture = client.capture_resource(src.WLTP_RESOURCE_ID, query={"filters": {"tozar": TOYOTA}})
    assert sum(len(page.records) for page in capture.pages) == capture.reported_total == 10_000
    assert count_tozar(client, TOYOTA, clock=clock) == 10_000
    # package_show + 10 pages + the final fresh count.
    assert len(transport.calls) == 12
    assert clock.now == pytest.approx(11 * 1.0 + LATENCY)
    with capsys.disabled():
        print(f"\nP51 capture of 10,000 rows: {len(transport.calls)} requests, {clock.now:.2f} s paced at 1 s")

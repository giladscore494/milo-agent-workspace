"""V1's internet search is MILO's, one admitted invocation at a time.

The property every test here is about is ORDER. V1 used to reach the internet
through Moonshot's builtin ``$web_search``: the provider executed it, inside a
chat request, as many times as it decided. MILO could reserve a worst case
before dispatch and reconcile afterwards, but it could never admit ONE search
-- so a response that searched once past the reviewed maximum had already
spent the money by the time MILO could see it.

Now the model is offered MILO's own ``web_search`` function tool, which the
provider cannot execute. Every ask returns to MILO, which takes the run's
search allowance and the endpoint's QPS bucket, charges the invocation
durably, and only then performs exactly one search. That makes
``max_search_invocations_per_run`` a ceiling a run cannot cross rather than a
total it can only report afterwards.

Mocked provider clients and injected search transports only -- no real
provider call and no real search is made anywhere in this file.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker
from backend.engines.vehicle_catalog_v1 import core
from backend.provider_authority import (ProviderAdapter,
                                        request_offers_builtin_search)
from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    QuotaConfig)
from backend.provider_scheduler import (ProviderBackpressureExceeded,
                                        ProviderLimitsConfig, ProviderScheduler)
from backend.standalone_search import (MAX_RESULTS_PER_SEARCH,
                                       MEDIATED_SEARCH_TOOL_NAME,
                                       PROVIDER_BUILTIN_SEARCH_NAME,
                                       STANDALONE_SEARCH_TIMEOUT_SECONDS,
                                       MoonshotStandaloneSearch, SearchOutcome,
                                       SearchResult, SearchTransportError,
                                       SearchUnavailable, normalize_results,
                                       request_offers_provider_executed_search)

# =============================================================================
# helpers
# =============================================================================


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def tool_call(name=MEDIATED_SEARCH_TOOL_NAME, query="hyundai tucson israel", *, call_id="c1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name,
                                 arguments=json.dumps({"query": query})))


def searching_response(*calls, prompt_tokens=10, completion_tokens=5):
    """A provider response asking MILO to run ``len(calls)`` searches."""
    message = SimpleNamespace(content="", tool_calls=list(calls),
                              model_dump=lambda **_: {"role": "assistant",
                                                      "tool_calls": []})
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens),
        choices=[SimpleNamespace(finish_reason="tool_calls", message=message)])


def final_response(content="{}", prompt_tokens=10, completion_tokens=5):
    message = SimpleNamespace(content=content, tool_calls=None,
                              model_dump=lambda **_: {"role": "assistant"})
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt_tokens,
                              completion_tokens=completion_tokens),
        choices=[SimpleNamespace(finish_reason="stop", message=message)])


def builtin_search_response():
    """A response from the world V1 used to live in: the provider ran it."""
    message = SimpleNamespace(
        content="",
        tool_calls=[SimpleNamespace(
            id="b1", function=SimpleNamespace(name=PROVIDER_BUILTIN_SEARCH_NAME,
                                              arguments="{}"))],
        model_dump=lambda **_: {"role": "assistant", "tool_calls": []})
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        choices=[SimpleNamespace(finish_reason="tool_calls", message=message)])


class ScriptedCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.outcomes:
            raise AssertionError("scripted provider client ran out of outcomes")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ScriptedClient:
    def __init__(self, outcomes):
        self.completions = ScriptedCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def requests(self):
        return self.completions.requests


class RecordingSearch:
    """A standalone search transport that records every invocation.

    It is the counter the ceiling tests read: `calls` is the number of
    searches that REALLY happened, which is the only number a post-facto
    ledger could not have been trusted about.
    """

    def __init__(self, results=None, *, fail_with=None, log=None):
        self.queries: list[str] = []
        self.endpoints: list[str] = []
        self._results = results
        self._fail_with = fail_with
        self._log = log

    @property
    def calls(self) -> int:
        return len(self.queries)

    def available(self) -> bool:
        return True

    def __call__(self, query, *, endpoint):
        self.queries.append(query)
        self.endpoints.append(endpoint)
        if self._log is not None:
            self._log.append(("execute", query))
        if self._fail_with is not None:
            raise self._fail_with
        if self._results is not None:
            return self._results
        return {"results": [
            {"title": "Hyundai Israel", "url": "https://hyundai.co.il/tucson",
             "snippet": "Tucson 2024 — ILS 179,900"},
        ]}


class OrderLoggingTracker(BudgetTracker):
    """A tracker that says WHEN each accounting step happened.

    Only ordering is observed; every decision is still the real one.
    """

    def __init__(self, *args, log, **kwargs):
        super().__init__(*args, **kwargs)
        self._log = log

    def reserve_search(self, count=1):
        try:
            seq = super().reserve_search(count)
        except BaseException:
            self._log.append(("reserve_refused", count))
            raise
        self._log.append(("reserve", count))
        return seq

    def settle_search(self, reservation_seq, actual=None, cost=None):
        super().settle_search(reservation_seq, actual=actual, cost=cost)
        self._log.append(("settle", actual))


def make_tracker(cls=BudgetTracker, **cfg):
    cfg.setdefault("estimated_cost_per_call", 0.0)
    cfg.setdefault("max_model_calls_per_run", 200)
    log = cfg.pop("log", None)
    config = BudgetConfig(**cfg)
    if log is not None:
        return OrderLoggingTracker(config, kill_switch=lambda: True, log=log)
    return cls(config, kill_switch=lambda: True)


def make_adapter(*, tracker, executor=None, quota=None, scheduler=None,
                 max_rate_limit_retries=3):
    clock = FakeClock()

    def sleep(seconds):
        clock.now += seconds

    coordinator = (ProviderQuotaCoordinator(MemoryQuotaBackend(), quota, clock=clock)
                   if quota is not None else None)
    scheduler = scheduler or ProviderScheduler(
        ProviderLimitsConfig(rpm_limit=None, tpm_limit=None,
                             max_rate_limit_retries=max_rate_limit_retries,
                             max_backpressure_wait_seconds=600.0),
        sleep_fn=sleep, clock=clock, rng=lambda: 0.0, coordinator=coordinator)
    adapter = ProviderAdapter(scheduler, tracker=tracker, search_executor=executor)
    return adapter, coordinator


@pytest.fixture
def v1(monkeypatch):
    """V1 wired to a scripted provider and an injected search transport.

    Returns a callable that runs ONE `moonshot_chat` and hands back the
    scripted client so the requests V1 really sent can be read.
    """
    def run(outcomes, *, tracker=None, executor=None, quota=None,
            use_web_search=True, scheduler=None, agent="discovery_agent",
            phase="discovery"):
        tracker = tracker if tracker is not None else make_tracker()
        executor = executor if executor is not None else RecordingSearch()
        adapter, coordinator = make_adapter(tracker=tracker, executor=executor,
                                            quota=quota, scheduler=scheduler)
        client = ScriptedClient(outcomes)
        monkeypatch.setattr(core, "PROVIDER_ADAPTER", adapter)
        monkeypatch.setattr(core, "PROVIDER_SCHEDULER", adapter.scheduler)
        monkeypatch.setattr(core, "MODEL_CLIENT_FACTORY",
                            lambda api_key, base_url: client)
        result = core.moonshot_chat(
            "test-key", [{"role": "user", "content": "find israeli models"}],
            temperature=0.6, use_web_search=use_web_search, max_tokens=500,
            agent_name=agent, phase_name=phase)
        return SimpleNamespace(result=result, client=client, tracker=tracker,
                               executor=executor, adapter=adapter,
                               coordinator=coordinator)
    return run


def tool_messages(request):
    return [m for m in request["messages"]
            if isinstance(m, dict) and m.get("role") == "tool"]


# =============================================================================
# 1. V1 PRODUCTION NO LONGER HANDS THE PROVIDER A SEARCH CAPABILITY
# =============================================================================

def test_v1_never_sends_a_provider_executed_search_tool(v1):
    """THE property this whole change exists for.

    Read off the requests V1 really sent, not off its source: a comment can
    say anything, and the payload is what the provider acts on.
    """
    run = v1([searching_response(tool_call()), final_response('{"ok":1}')])
    assert run.client.requests, "V1 sent no provider request at all"
    for request in run.client.requests:
        assert not request_offers_builtin_search(request)
        assert not request_offers_provider_executed_search(request)
        for tool in request.get("tools") or ():
            assert tool["type"] == "function"
            assert tool["function"]["name"] == MEDIATED_SEARCH_TOOL_NAME
            assert not tool["function"]["name"].startswith("$")


def test_every_v1_production_agent_offers_only_the_mediated_tool(monkeypatch):
    """The sweep: every V1 entry point that researches, not just one path."""
    tracker = make_tracker(max_search_invocations_per_run=60)
    adapter, _ = make_adapter(tracker=tracker, executor=RecordingSearch())
    client = ScriptedClient([final_response(json.dumps({
        "agent": "a", "models": [{"model_name_en": "Tucson",
                                  "source_url": "https://x.co.il"}],
        "items": [], "missing_data": [], "extra_candidate_models": [],
    }))] * 12)
    monkeypatch.setattr(core, "PROVIDER_ADAPTER", adapter)
    monkeypatch.setattr(core, "PROVIDER_SCHEDULER", adapter.scheduler)
    monkeypatch.setattr(core, "MODEL_CLIENT_FACTORY", lambda k, u: client)

    core.run_discovery_agent("k", core.DISCOVERY_AGENTS[0], "Hyundai", "Israel", "2010")
    core.run_technical_agent("k", core.TECHNICAL_AGENTS[0], "Hyundai", "Israel",
                             "2010", [{"model_name_en": "Tucson"}])

    assert client.requests
    assert any(request.get("tools") for request in client.requests), (
        "no V1 research request offered a search tool at all")
    for request in client.requests:
        assert not request_offers_provider_executed_search(request)


def test_the_builtin_name_has_no_power_in_the_mediated_loop(v1):
    """NEGATIVE CONTROL: the old name is now just a string.

    A model that asks for `$web_search` is refused. Nothing is echoed back --
    and the echo IS what used to ask Moonshot to run the search -- so no
    search happens and none is charged.
    """
    run = v1([builtin_search_response(), final_response()])
    assert run.executor.calls == 0
    assert run.tracker.search_invocations == 0
    answered = tool_messages(run.client.requests[1])
    assert len(answered) == 1
    payload = json.loads(answered[0]["content"])
    assert payload["status"] == "error"
    assert payload["tool"] == PROVIDER_BUILTIN_SEARCH_NAME
    # And the refusal is a refusal, not the arguments handed back.
    assert "arguments" not in answered[0]["content"]


# =============================================================================
# 2. INTERNET SEARCH STILL HAPPENS, THROUGH THE STANDALONE MECHANISM
# =============================================================================

def test_internet_search_still_happens_through_the_standalone_path(v1):
    run = v1([searching_response(tool_call(query="hyundai ioniq 5 israel price")),
              final_response('{"models":[]}')])
    assert run.executor.calls == 1, "no internet search was performed"
    assert run.executor.queries == ["hyundai ioniq 5 israel price"]
    assert run.executor.endpoints == ["search"]
    assert run.tracker.search_invocations == 1


def test_the_standalone_transport_posts_one_request_to_the_search_endpoint():
    """The transport performs ONE search and reads results out of the reply.

    A loopback double, never the provider: what is under test is that one
    admitted invocation is one HTTP request to the standalone endpoint, and
    that the reply becomes bounded result material.
    """
    posts = []

    class FakeHttp:
        def post(self, url, *, headers, json):
            posts.append((url, headers, json))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"search_results": [
                    {"title": "t", "url": "https://example.co.il", "snippet": "s"}]})

    transport = MoonshotStandaloneSearch(api_key="k", base_url="https://api.test/v1",
                                         http_client=FakeHttp())
    assert transport.available() is True
    results = transport("tucson israel", endpoint="search")
    assert len(posts) == 1, "one admitted search made more than one request"
    url, headers, body = posts[0]
    assert url == "https://api.test/v1/tools/search"
    assert headers["Authorization"] == "Bearer k"
    assert body == {
        "text_query": "tucson israel",
        "limit": MAX_RESULTS_PER_SEARCH,
        "timeout_seconds": STANDALONE_SEARCH_TIMEOUT_SECONDS,
    }
    assert body["limit"] == 8
    assert body["timeout_seconds"] == 30
    assert normalize_results({"search_results": []}) == ()
    assert results[0].url == "https://example.co.il"


def test_a_transport_with_no_credential_is_not_available():
    assert MoonshotStandaloneSearch(api_key="").available() is False


def test_the_transport_builds_one_http_client_and_reuses_it(monkeypatch):
    """A fresh connection pool per search is a socket leak, not isolation."""
    import backend.budget as budget

    built = []

    class FakeHttp:
        def post(self, url, *, headers, json):
            return SimpleNamespace(status_code=200, json=lambda: {"results": []})

    def build(deadline=None):
        built.append(deadline)
        return FakeHttp()

    monkeypatch.setattr(budget, "build_provider_http_client", build)
    transport = MoonshotStandaloneSearch(api_key="k", base_url="https://api.test/v1")
    for _ in range(3):
        transport("q", endpoint="search")
    assert len(built) == 1, f"a client was rebuilt per search: {len(built)}"


def test_an_unknown_endpoint_is_refused_by_the_transport():
    transport = MoonshotStandaloneSearch(api_key="k")
    with pytest.raises(SearchTransportError):
        transport("q", endpoint="not_an_endpoint")


def test_an_http_error_from_the_search_endpoint_is_a_transport_failure():
    class Failing:
        def post(self, url, *, headers, json):
            return SimpleNamespace(status_code=503, json=lambda: {})

    transport = MoonshotStandaloneSearch(api_key="k", base_url="https://api.test/v1",
                                         http_client=Failing())
    with pytest.raises(SearchTransportError):
        transport("q", endpoint="search")


# =============================================================================
# 3. ADMITTED BEFORE IT EXECUTES
# =============================================================================

def test_every_search_is_admitted_before_it_executes(v1):
    """Order, not outcome: the allowance is taken BEFORE the search runs."""
    log: list[tuple] = []
    tracker = make_tracker(max_search_invocations_per_run=10, log=log)
    executor = RecordingSearch(log=log)
    run = v1([searching_response(tool_call()), final_response()],
             tracker=tracker, executor=executor)
    assert run.executor.calls == 1
    kinds = [entry[0] for entry in log]
    assert kinds == ["reserve", "settle", "execute"], (
        f"a search executed before it was admitted and charged: {kinds}")


def test_the_qps_bucket_is_taken_before_the_search_executes():
    """And the endpoint's own bucket is consumed by an admitted search."""
    tracker = make_tracker(max_search_invocations_per_run=10)
    executor = RecordingSearch()
    adapter, coordinator = make_adapter(tracker=tracker, executor=executor,
                                        quota=QuotaConfig())
    adapter.run_search({"query": "tucson"}, agent="a", phase="p")
    assert executor.calls == 1
    assert not coordinator.try_admit_search("search")[0], (
        "an admitted search did not consume the endpoint's QPS bucket")


def test_an_unconfigured_transport_refuses_before_charging_anything():
    """NEGATIVE CONTROL: no transport is a refusal, never a silent fallback.

    The refusal lands before admission, so a run that cannot search is not
    debited for searches it could never perform -- and, far more importantly,
    nothing reaches for the provider-executed capability instead.
    """
    tracker = make_tracker(max_search_invocations_per_run=5)
    adapter, _ = make_adapter(tracker=tracker, executor=None)
    with pytest.raises(SearchUnavailable):
        adapter.run_search({"query": "tucson"}, executor=None)
    assert tracker.search_invocations == 0
    assert tracker.reserved_search_invocations == 0


def test_a_search_cannot_happen_where_nothing_can_account_for_it():
    """NEGATIVE CONTROL: no ledger, no search.

    `max_search_invocations_per_run` is enforced by the run's ledger, so an
    adapter without one could only perform UNMETERED searches -- the builtin's
    failure mode wearing different clothes. The production worker always
    installs the ledger; this makes the guarantee structural rather than a
    property of the wiring.
    """
    executor = RecordingSearch()
    scheduler = ProviderScheduler(ProviderLimitsConfig())
    adapter = ProviderAdapter(scheduler, tracker=None, search_executor=executor)
    with pytest.raises(SearchUnavailable):
        adapter.run_search({"query": "tucson"})
    assert executor.calls == 0


def test_a_malformed_ask_is_refused_before_admission(v1):
    """An empty query is a malformed request, not a search the run spent."""
    empty = SimpleNamespace(id="c1", function=SimpleNamespace(
        name=MEDIATED_SEARCH_TOOL_NAME, arguments='{"query": "   "}'))
    run = v1([searching_response(empty), final_response()])
    assert run.executor.calls == 0
    assert run.tracker.search_invocations == 0
    payload = json.loads(tool_messages(run.client.requests[1])[0]["content"])
    assert payload["status"] == "error"


# =============================================================================
# 4-5. THE RUN-LEVEL HARD CEILING
# =============================================================================

def test_with_one_search_left_only_one_more_can_execute(v1):
    """Two asks in one response, one invocation left: exactly one runs.

    The second is refused BEFORE it executes, which is the difference between
    a ceiling and a report. Under the builtin this case was unreachable: the
    provider decided how many searches its own response performed.
    """
    tracker = make_tracker(max_search_invocations_per_run=1)
    with pytest.raises(BudgetExceeded) as refused:
        v1([searching_response(tool_call(call_id="a", query="first"),
                               tool_call(call_id="b", query="second")),
            final_response()], tracker=tracker)
    assert refused.value.code == "SEARCH_LIMIT_REACHED"
    assert tracker.search_invocations == 1
    assert tracker.reserved_search_invocations == 0


def test_the_refused_second_search_never_reached_the_transport(v1):
    executor = RecordingSearch()
    tracker = make_tracker(max_search_invocations_per_run=1)
    with pytest.raises(BudgetExceeded):
        v1([searching_response(tool_call(call_id="a", query="first"),
                               tool_call(call_id="b", query="second")),
            final_response()], tracker=tracker, executor=executor)
    assert executor.calls == 1, "a search ran with no allowance left"
    assert executor.queries == ["first"]


@pytest.mark.parametrize("per_response", [1, 2, 3])
def test_the_run_search_ceiling_is_never_crossed(per_response):
    """Driven to exhaustion, and the transport's own count is the witness.

    The sweep matters because a single case can pass by luck. However many
    searches each response asks for, the number that REALLY executed lands on
    or under the ceiling -- never one over, not even transiently.
    """
    ceiling = 7
    tracker = make_tracker(max_search_invocations_per_run=ceiling)
    executor = RecordingSearch()
    adapter, _ = make_adapter(tracker=tracker, executor=executor)

    refusals = 0
    for _ in range(ceiling + 5):
        try:
            for index in range(per_response):
                adapter.run_search({"query": f"q{index}"})
        except BudgetExceeded as exc:
            assert exc.code == "SEARCH_LIMIT_REACHED"
            refusals += 1
            break
        assert executor.calls <= ceiling, (
            f"the run crossed its search ceiling: {executor.calls} > {ceiling}")
        assert tracker.search_invocations == executor.calls, (
            "the ledger and the transport disagree about how many searches ran")
    assert refusals == 1, "the sequence never reached the ceiling at all"
    assert executor.calls <= ceiling
    assert tracker.reserved_search_invocations == 0


def test_two_threads_cannot_both_take_the_last_search(v1):
    """A read-then-check would let both through; a reservation does not."""
    tracker = make_tracker(max_search_invocations_per_run=1)
    executor = RecordingSearch()
    adapter, _ = make_adapter(tracker=tracker, executor=executor)
    start = threading.Barrier(2)
    refused: list[BaseException] = []

    def attempt():
        start.wait()
        try:
            adapter.run_search({"query": "same last slot"})
        except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
            refused.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert executor.calls == 1, "two threads both performed the last search"
    assert len(refused) == 1
    assert isinstance(refused[0], BudgetExceeded)


# =============================================================================
# 6-7. RETRY AND RESUME NEVER HAND A PERFORMED SEARCH BACK
# =============================================================================

def test_a_retry_cannot_refund_a_search_that_already_happened(v1):
    """Spent search only ever grows across the attempts of one call.

    The chat request is retried after backpressure; the searches the first
    round performed are already spent, and the retry must start from there
    rather than from where the call began.
    """
    tracker = make_tracker(max_search_invocations_per_run=20)
    executor = RecordingSearch()
    seen: list[int] = []

    class Sampling(RecordingSearch):
        def __call__(self, query, *, endpoint):
            seen.append(tracker.search_invocations)
            return RecordingSearch.__call__(self, query, endpoint=endpoint)

    sampling = Sampling()
    rate_limited = Exception("Error code: 429 - rate_limit_reached_error")
    rate_limited.status_code = 429
    rate_limited.response = SimpleNamespace(status_code=429, headers={})

    run = v1([searching_response(tool_call(call_id="a", query="one")),
              rate_limited,
              searching_response(tool_call(call_id="b", query="two")),
              final_response()],
             tracker=tracker, executor=sampling)

    assert sampling.calls == 2
    # The first search was charged BEFORE it ran, so the second sees it.
    assert seen == [1, 2], f"an attempt saw refunded search spend: {seen}"
    assert seen == sorted(seen), "settled search spend went DOWN across a retry"
    assert tracker.search_invocations == 2
    assert tracker.reserved_search_invocations == 0
    assert run.tracker.reserved_search_invocations == 0


def test_a_resume_restores_already_spent_search_usage():
    """Restoring is not a refund: the remaining allowance shrinks with it."""
    ceiling = 4
    before = make_tracker(max_search_invocations_per_run=ceiling)
    adapter, _ = make_adapter(tracker=before, executor=RecordingSearch())
    adapter.run_search({"query": "one"})
    adapter.run_search({"query": "two"})
    durable = before.ledger_snapshot()
    assert durable["search_invocations"] == 2

    after = make_tracker(max_search_invocations_per_run=ceiling)
    after.restore_snapshot({k: v for k, v in durable.items()})
    assert after.search_invocations == 2, "a resume lost searches the run had spent"

    executor = RecordingSearch()
    resumed, _ = make_adapter(tracker=after, executor=executor)
    resumed.run_search({"query": "three"})
    resumed.run_search({"query": "four"})
    with pytest.raises(BudgetExceeded) as refused:
        resumed.run_search({"query": "five"})
    assert refused.value.code == "SEARCH_LIMIT_REACHED"
    assert executor.calls == 2, "a resumed run performed searches it had no room for"
    assert after.search_invocations == ceiling


def test_a_crash_between_admission_and_the_search_cannot_return_the_slot():
    """The charge is committed BEFORE the search, so nothing can undo it.

    A settlement that waited for the reply could be lost to a process that
    died mid-search, and a performed search that no longer appears in the
    ledger is a refund by another name. Charging first can only over-report,
    which is the fail-closed direction.
    """
    class Dies(RecordingSearch):
        def __call__(self, query, *, endpoint):
            self.queries.append(query)
            raise KeyboardInterrupt("the worker died mid-search")

    tracker = make_tracker(max_search_invocations_per_run=3)
    adapter, _ = make_adapter(tracker=tracker, executor=Dies())
    with pytest.raises(KeyboardInterrupt):
        adapter.run_search({"query": "one"})
    assert tracker.search_invocations == 1, (
        "a search that was admitted and begun was handed back")
    durable = tracker.ledger_snapshot()
    assert durable["search_invocations"] == 1


# =============================================================================
# 8-9. REFUSALS AND FAILURES, ON THE RIGHT SIDE OF THE CHARGE
# =============================================================================

def test_a_qps_refusal_does_not_charge_a_search_that_never_happened():
    """The run could pay; the provider bucket said no. Nothing ran."""
    class Refuses:
        config = ProviderLimitsConfig()

        def admit_search(self, endpoint, *, agent="", phase="", max_wait_seconds=None):
            raise ProviderBackpressureExceeded("search QPS did not clear")

    tracker = make_tracker(max_search_invocations_per_run=3)
    executor = RecordingSearch()
    adapter = ProviderAdapter(Refuses(), tracker=tracker, search_executor=executor)
    with pytest.raises(ProviderBackpressureExceeded):
        adapter.run_search({"query": "tucson"})
    assert executor.calls == 0
    assert tracker.search_invocations == 0
    assert tracker.reserved_search_invocations == 0
    assert tracker.reserve_search(3), "the refusal stranded the run's allowance"


def test_a_failed_search_with_an_uncertain_outcome_stays_charged(v1):
    """FAIL CLOSED. MILO cannot know whether the provider ran it first.

    An unknown amount of provider spend is not an absence of spend: refunding
    here would make the ceiling a number a flaky transport could reset.
    """
    executor = RecordingSearch(fail_with=SearchTransportError("upstream reset"))
    tracker = make_tracker(max_search_invocations_per_run=5)
    run = v1([searching_response(tool_call()), final_response()],
             tracker=tracker, executor=executor)
    assert executor.calls == 1
    assert tracker.search_invocations == 1, "a failed search was refunded"
    payload = json.loads(tool_messages(run.client.requests[1])[0]["content"])
    assert payload["status"] == "error"
    assert "Do NOT invent sources" in payload["instruction"]


def test_a_failed_search_never_tells_the_model_it_found_nothing_real(v1):
    """An error is an error: never an empty, successful-looking result set."""
    executor = RecordingSearch(fail_with=RuntimeError("boom"))
    run = v1([searching_response(tool_call()), final_response()],
             tracker=make_tracker(max_search_invocations_per_run=5),
             executor=executor)
    payload = json.loads(tool_messages(run.client.requests[1])[0]["content"])
    assert payload["status"] == "error"
    assert "results" not in payload


def test_a_run_level_stop_is_never_reported_to_the_model_as_a_search_result():
    """A budget stop from inside the transport ends the run, not the search."""
    stop = BudgetExceeded("COST_LIMIT_EXCEEDED", "stop", "budget_exhausted",
                          "budget_exhausted")
    tracker = make_tracker(max_search_invocations_per_run=5)
    adapter, _ = make_adapter(tracker=tracker,
                              executor=RecordingSearch(fail_with=stop))
    with pytest.raises(BudgetExceeded):
        adapter.run_search({"query": "tucson"})


# =============================================================================
# 10. THE RESULTS REACH THE MODEL
# =============================================================================

def test_search_results_are_handed_back_into_the_v1_model_flow(v1):
    """The point of the whole path: the model still researches the internet."""
    executor = RecordingSearch(results={"results": [
        {"title": "Hyundai Israel", "url": "https://hyundai.co.il/tucson",
         "snippet": "Tucson 2024 — ILS 179,900"},
        {"title": "Portal", "url": "https://auto.co.il/tucson", "snippet": "specs"},
    ]})
    run = v1([searching_response(tool_call(query="tucson israel price")),
              final_response('{"models":[{"model_name_en":"Tucson"}]}')],
             executor=executor)

    # The follow-up request carries the tool result...
    follow_up = run.client.requests[1]
    messages = tool_messages(follow_up)
    assert len(messages) == 1
    assert messages[0]["name"] == MEDIATED_SEARCH_TOOL_NAME
    payload = json.loads(messages[0]["content"])
    assert payload["status"] == "ok"
    assert payload["result_count"] == 2
    assert payload["results"][0]["source_url"] == "https://hyundai.co.il/tucson"
    assert "ILS 179,900" in payload["results"][0]["snippet"]
    # ...and V1 returns the answer the model gave after reading it.
    assert run.result["content"] == '{"models":[{"model_name_en":"Tucson"}]}'
    assert run.result["finish_reason"] == "stop"


def test_result_material_is_bounded_before_it_re_enters_a_prompt():
    """Model-facing material is server-bounded, however large the reply."""
    outcome = SearchOutcome(
        query="q", endpoint="search",
        results=tuple(SearchResult(title="t" * 50, url="https://e/%d" % i,
                                   snippet="s" * 5_000) for i in range(8)))
    content = outcome.as_tool_content()
    assert len(content) <= 12_000
    payload = json.loads(content)
    assert payload["truncated"] is True
    assert payload["result_count"] < 8


def test_the_transport_never_invents_results_from_a_shape_it_cannot_read():
    assert normalize_results("not a result set") == ()
    assert normalize_results({"unexpected": {"deeply": "nested"}}) == ()
    assert normalize_results(None) == ()


# =============================================================================
# 11. EVERYTHING ABOUT V1 THAT IS NOT SEARCH
# =============================================================================

def test_a_non_searching_agent_offers_no_tool_and_spends_no_search(v1):
    run = v1([final_response('{"agent":"normalizer"}')], use_web_search=False)
    assert run.executor.calls == 0
    assert run.tracker.search_invocations == 0
    for request in run.client.requests:
        assert "tools" not in request
    assert run.result["content"] == '{"agent":"normalizer"}'


def test_an_agent_without_internet_cannot_search_by_asking(v1):
    """NEGATIVE CONTROL: the grant is the gate, and the gate is real.

    A phase that was never given internet does not get it by emitting a tool
    call for the tool it was not offered.
    """
    run = v1([searching_response(tool_call()), final_response()],
             use_web_search=False)
    assert run.executor.calls == 0
    assert run.tracker.search_invocations == 0
    payload = json.loads(tool_messages(run.client.requests[1])[0]["content"])
    assert payload["status"] == "error"
    assert "no internet access" in payload["error"]


def test_the_preserved_result_shape_and_token_accounting_are_unchanged(v1):
    run = v1([searching_response(tool_call()), final_response("done", prompt_tokens=7,
                                                              completion_tokens=3)])
    assert set(run.result) == {"content", "finish_reason", "input_tokens",
                               "output_tokens", "parsed", "agent", "phase"}
    assert run.result["input_tokens"] == 17   # 10 from the search round + 7
    assert run.result["output_tokens"] == 8   # 5 + 3
    assert run.result["agent"] == "discovery_agent"
    assert run.result["phase"] == "discovery"


def test_the_tool_loop_is_still_bounded_by_max_tool_rounds(v1):
    """A model that only ever asks cannot loop forever -- preserved bound."""
    tracker = make_tracker(max_search_invocations_per_run=100)
    run = v1([searching_response(tool_call())] * core.MAX_TOOL_ROUNDS,
             tracker=tracker)
    assert len(run.client.requests) == core.MAX_TOOL_ROUNDS
    assert run.executor.calls == core.MAX_TOOL_ROUNDS


# =============================================================================
# 12. THE PROVIDER AUTHORITY IS STILL ONE AUTHORITY
# =============================================================================

def test_the_residual_builtin_accounting_still_holds_for_any_caller():
    """Nothing in production offers the builtin -- but the net stays up.

    If any caller ever sends one, the authority still reserves the reviewed
    worst case before dispatch and charges what really happened.
    """
    tracker = make_tracker(max_search_invocations_per_run=4,
                           max_builtin_searches_per_request=4)
    adapter, _ = make_adapter(tracker=tracker, executor=RecordingSearch())
    builtin_request = {
        "model": "kimi-k2.6", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "tools": [{"type": "builtin_function",
                   "function": {"name": PROVIDER_BUILTIN_SEARCH_NAME}}],
    }
    client = ScriptedClient([builtin_search_response()])
    adapter.chat(builtin_request, client=client)
    assert tracker.search_invocations == 1
    with pytest.raises(BudgetExceeded) as refused:
        adapter.chat(builtin_request, client=ScriptedClient([builtin_search_response()]))
    assert refused.value.code == "SEARCH_LIMIT_REACHED"


def test_one_adapter_serves_the_mediated_search_and_both_engines():
    """Search is not a second authority beside the provider one."""
    from backend.engines.swarm_v2.model_gateway import ModelGateway
    from backend.engines.vehicle_catalog_v1.engine import VehicleCatalogEngine

    executor = RecordingSearch()
    tracker = make_tracker()
    adapter, _ = make_adapter(tracker=tracker, executor=executor)
    engine = VehicleCatalogEngine(provider_adapter=adapter)
    engine._install_injections()
    try:
        gateway = ModelGateway(guarded_client_factory=lambda k, u: ScriptedClient([]),
                               adapter=adapter, api_key="k", base_url="u")
        assert core.PROVIDER_ADAPTER is adapter
        assert core._provider_authority() is adapter
        assert gateway._adapter is adapter
        assert adapter.search_executor is executor
    finally:
        engine._restore_injections()


def test_the_worker_gives_its_one_adapter_the_one_search_transport():
    import inspect

    from backend.worker import main as worker_main

    source = inspect.getsource(worker_main)
    assert source.count("ProviderAdapter(") == 1
    assert source.count("build_default_search_executor(") == 1
    assert "search_executor=build_default_search_executor(" in source
    # And that one adapter carries the RUN's ledger, or the searches it
    # admits would be paced but never counted against the run ceiling.
    assert "tracker=tracker," in source


def test_v1_states_no_search_mechanic_of_its_own():
    """Admission, pacing, accounting and the transport are the authority's."""
    import inspect

    source = inspect.getsource(core)
    assert "admit_search" not in source, "V1 paces search itself"
    assert "reserve_search" not in source, "V1 accounts for search itself"
    assert "record_search" not in source
    assert '"builtin_function"' not in source

"""ONE provider authority: the regressions that say it really is one.

Every test here is about a property that was NOT true before this module
existed, or that was true only by coincidence because two surfaces happened
to agree. Mocked provider clients and loopback servers only -- no real
provider call is made anywhere in this file.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, GuardedModelClient
from backend.provider_authority import (AUTHORITATIVE_BASIS, BUILTIN_WEB_SEARCH,
                                        CONSERVATIVE_BASIS, ProviderAdapter,
                                        ProviderOutcome, TokenCeilingExceeded,
                                        UnknownTokenDemand, admission_demand,
                                        classify_outcome,
                                        conservative_input_tokens,
                                        register_token_counter)
from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    QuotaConfig)
from backend.provider_scheduler import (ProviderBackpressureExceeded,
                                        ProviderLimitsConfig, ProviderScheduler,
                                        estimate_input_tokens)
from backend.provider_transport import (ProviderRequestDeadlineExceeded,
                                        allocate_request_timeouts,
                                        build_deadline_http_client)
from backend.runtime import CancellationRequested


# =============================================================================
# helpers
# =============================================================================

class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def status_error(message: str, status: int, *, headers: dict | None = None):
    """An exception shaped like the OpenAI SDK's APIStatusError family.

    It carries a response OBJECT, which is the STRUCTURAL evidence that the
    provider really answered -- the only thing that lets a permit go back.
    """
    response = SimpleNamespace(status_code=status, headers=headers or {})
    exc = Exception(message)
    exc.status_code = status
    exc.response = response
    return exc


def response_with(content="{}", *, searches=0, prompt_tokens=10, completion_tokens=5):
    tool_calls = [SimpleNamespace(
        id=f"call-{i}", function=SimpleNamespace(name=BUILTIN_WEB_SEARCH, arguments="{}"))
        for i in range(searches)]
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        choices=[SimpleNamespace(finish_reason="tool_calls" if searches else "stop",
                                 message=message)])


class ScriptedCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ScriptedClient:
    def __init__(self, outcomes):
        self.completions = ScriptedCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def calls(self):
        return len(self.completions.requests)


def make_tracker(**cfg):
    cfg.setdefault("estimated_cost_per_call", 0.0)
    return BudgetTracker(BudgetConfig(**cfg), kill_switch=lambda: True)


def make_adapter(outcomes, *, tracker=None, quota=None, limits=None,
                 guarded=True, max_rate_limit_retries=3):
    """An adapter, its client, and (optionally) an organization coordinator.

    ONE fake clock drives the scheduler AND the coordinator. They must share
    it: the coordinator PAUSES a dimension when it sees a 429, and a paused
    dimension that ages out on the wall clock while the scheduler waits on a
    fake one never clears.
    """
    clock = FakeClock()

    def sleep(seconds):
        clock.now += seconds

    coordinator = None
    if quota is not None:
        coordinator = ProviderQuotaCoordinator(MemoryQuotaBackend(), quota, clock=clock)
    tracker = tracker if tracker is not None else make_tracker()
    scheduler = ProviderScheduler(
        limits or ProviderLimitsConfig(rpm_limit=None, tpm_limit=None,
                                       max_rate_limit_retries=max_rate_limit_retries,
                                       max_backpressure_wait_seconds=600.0),
        sleep_fn=sleep, clock=clock, rng=lambda: 0.0, coordinator=coordinator)
    inner = ScriptedClient(outcomes)
    client = GuardedModelClient(inner, tracker) if guarded else inner
    adapter = ProviderAdapter(scheduler, tracker=tracker)
    return adapter, client, inner, tracker, coordinator


MESSAGES = [{"role": "user", "content": "hello"}]
REQUEST = {"model": "kimi-k2.6", "messages": MESSAGES, "max_tokens": 100}


# =============================================================================
# 1. ONE taxonomy: a 503 is backpressure EVERYWHERE, and is charged once
# =============================================================================

@pytest.mark.parametrize("message,status", [
    ("Error code: 503 - engine_overloaded_error", 503),
    ("the engine is overloaded right now", 503),
    ("Error code: 429 - rate_limit_reached_error", 429),
])
def test_provider_backpressure_never_consumes_a_semantic_retry(message, status):
    """THE regression this module exists for.

    Before it, `is_provider_rate_limit_error` (the ledger's classifier) did
    not recognise 503/`engine_overloaded_error` at all, while
    `classify_provider_error` (the scheduler's) did. So the scheduler paced
    and retried a 503 as backpressure AND the ledger charged the very same
    event against `max_retries`. One provider event, two verdicts, two
    counters -- and a run that could die at RETRY_LIMIT_REACHED with no model
    having misbehaved.
    """
    verdict = classify_outcome(status_error(message, status))
    assert verdict.outcome is ProviderOutcome.RATE_LIMIT
    assert verdict.is_backpressure
    assert not verdict.consumes_semantic_retry

    tracker = make_tracker(max_retries=1)
    guarded = GuardedModelClient(ScriptedClient([status_error(message, status)] * 4), tracker)
    for _ in range(4):
        with pytest.raises(Exception):
            guarded.chat.completions.create(**REQUEST)
    assert tracker.retries == 0
    assert tracker.provider_backpressure_events == 4
    assert tracker.stop is None, "backpressure tripped the semantic retry ceiling"


def test_the_ledger_and_the_scheduler_read_the_same_verdict():
    """Not "they agree today" -- they ask the same function."""
    from backend import budget as budget_module
    from backend import provider_scheduler as scheduler_module

    assert "classify_outcome" in budget_module.__dict__
    assert "classify_outcome" in scheduler_module.__dict__
    assert budget_module.classify_outcome is scheduler_module.classify_outcome
    assert budget_module.classify_outcome is classify_outcome


@pytest.mark.parametrize("exc,outcome,proven", [
    (None, ProviderOutcome.SUCCESS, True),
    (status_error("Error code: 429", 429), ProviderOutcome.RATE_LIMIT, True),
    (status_error("Error code: 503 - engine_overloaded_error", 503),
     ProviderOutcome.RATE_LIMIT, True),
    (status_error("Error code: 500 - internal", 500), ProviderOutcome.RETRYABLE_FAILURE, True),
    (status_error("Error code: 400 - bad request", 400), ProviderOutcome.NON_RETRYABLE_FAILURE, True),
    (ProviderRequestDeadlineExceeded(90.0, 91.2), ProviderOutcome.TIMEOUT, False),
    (CancellationRequested("RUN_CANCELLED"), ProviderOutcome.CANCELLATION, False),
    (RuntimeError("something nobody named"), ProviderOutcome.UNKNOWN, False),
])
def test_the_taxonomy_places_every_outcome_exactly_once(exc, outcome, proven):
    verdict = classify_outcome(exc)
    assert verdict.outcome is outcome
    assert verdict.completion_proven is proven


def test_a_quota_exhaustion_is_terminal_and_charges_no_retry():
    """Retrying cannot make quota appear, and a counter that fills up because
    the account is out of money tells nobody anything true."""
    from backend.provider_scheduler import ProviderQuotaExceeded

    exc = status_error("Error code: 429 - exceeded_current_quota_error", 429)
    verdict = classify_outcome(exc)
    assert verdict.is_quota_exhaustion
    assert not verdict.consumes_semantic_retry
    assert not verdict.is_backpressure

    adapter, client, inner, tracker, _coordinator = make_adapter([exc])
    with pytest.raises(ProviderQuotaExceeded):
        adapter.chat(REQUEST, client=client)
    assert inner.calls == 1, "a hard quota refusal was retried"
    assert tracker.retries == 0


# =============================================================================
# 2. Retry accounting: every attempt re-enters admission AND the ledger
# =============================================================================

def test_every_retry_re_enters_admission_and_cumulative_accounting():
    """A retry is a REAL provider attempt and is accounted like one.

    Three things must be true of attempt N, not just of attempt 1: it is
    admitted by the organization gate, it consumes the run's model-call and
    attempt ledger, and it is paced rather than charged to `max_retries`.
    """
    tracker = make_tracker(max_model_calls_per_run=10, max_retries=1)
    adapter, client, inner, _, coordinator = make_adapter(
        [status_error("Error code: 429", 429),
         status_error("Error code: 503 - engine_overloaded_error", 503),
         response_with()],
        tracker=tracker,
        quota=QuotaConfig(max_concurrency=4, max_rpm=10, max_tpm=1_000_000))

    result = adapter.chat(REQUEST, client=client)
    assert result is not None
    assert inner.calls == 3

    # The organization RPM window saw THREE requests, not one.
    admitted = 0
    while coordinator.try_admit_request(1)[0]:
        admitted += 1
    assert admitted == 10 - 3, "retries did not re-enter the organization gate"

    # And the run's durable ledger counted all three.
    ledger = tracker.ledger_snapshot()
    assert ledger["model_calls"] == 3
    assert ledger["provider_attempts"] == 3
    assert ledger["provider_failures"] == 2
    assert ledger["retries"] == 0, "backpressure consumed a semantic retry"
    assert ledger["provider_backpressure_events"] == 2


def test_a_retry_cannot_outrun_the_runtime_policy_attempt_ceiling():
    """RuntimePolicy stays the limit authority for how many attempts happen."""
    from backend.runtime_policy import reviewed_first_run_policy

    policy = reviewed_first_run_policy()
    limits = policy.provider_limits()
    assert limits.max_rate_limit_retries == policy["provider_max_rate_limit_retries"]
    assert policy.max_provider_attempts_per_call == 1 + limits.max_rate_limit_retries

    tracker = make_tracker(max_model_calls_per_run=100)
    adapter, client, inner, _, coordinator = make_adapter(
        [status_error("Error code: 429", 429)] * 20, tracker=tracker,
        limits=ProviderLimitsConfig(
            rpm_limit=None, tpm_limit=None,
            max_rate_limit_retries=limits.max_rate_limit_retries,
            max_backpressure_wait_seconds=10_000.0))
    with pytest.raises(ProviderBackpressureExceeded):
        adapter.chat(REQUEST, client=client)
    assert inner.calls == policy.max_provider_attempts_per_call


def test_the_sdk_never_retries_on_milos_behalf():
    """SDK retries are invisible to every MILO counter and limiter.

    Two silent SDK retries turn one logical request into three provider
    attempts that spend organization RPM and concurrency while the ledger
    reads clean. The one place that builds provider clients sets zero.
    """
    import ast
    import inspect

    from backend import budget as budget_module
    from backend import provider_authority

    sources = [inspect.getsource(budget_module), inspect.getsource(provider_authority)]
    constructions = 0
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "OpenAI"):
                constructions += 1
                keywords = {kw.arg: kw.value for kw in node.keywords}
                assert "http_client" in keywords, "a client with no total-deadline transport"
                retries = keywords["max_retries"]
                assert isinstance(retries, ast.Constant) and retries.value == 0
    assert constructions >= 1

    adapter = ProviderAdapter(ProviderScheduler(ProviderLimitsConfig()))
    built = adapter.default_client_factory("k", "https://example.invalid/v1")
    assert built.max_retries == 0


# =============================================================================
# 3. Token admission: a BOUND, never an average
# =============================================================================

def test_the_admission_rule_is_a_bound_not_an_average():
    """`chars // 4` is an average for English prose, and this product's
    market writes Hebrew -- 2 UTF-8 bytes per character, and a byte-level BPE
    tokenizer can emit one token per byte. The old rule could under-count real
    input by up to 8x, against a HARD shared ceiling."""
    hebrew = [{"role": "user", "content": "יונדאי טוסון" * 200}]
    optimistic = estimate_input_tokens(hebrew)
    bound = conservative_input_tokens(hebrew)
    utf8_bytes = len(json.dumps(hebrew, ensure_ascii=False).encode("utf-8"))

    assert bound >= utf8_bytes, "the bound is below the true byte count"
    assert bound > optimistic * 4, "the bound is no more conservative than the average"


def test_a_request_that_under_estimates_cannot_breach_a_hard_tpm_ceiling():
    """The ceiling binds on the BOUND, not on the optimistic estimate."""
    hebrew = [{"role": "user", "content": "יונדאי טוסון" * 100}]
    request = {"model": "kimi-k2.6", "messages": hebrew, "max_tokens": 100}
    optimistic = estimate_input_tokens(hebrew) + 100
    bound = admission_demand(hebrew, 100).tokens
    # A ceiling the optimistic estimate fits under and the real demand does not.
    ceiling = (optimistic + bound) // 2
    assert optimistic <= ceiling < bound

    adapter, client, inner, _, coordinator = make_adapter(
        [response_with()],
        limits=ProviderLimitsConfig(rpm_limit=None, tpm_limit=ceiling,
                                    max_backpressure_wait_seconds=10.0))
    with pytest.raises(TokenCeilingExceeded):
        adapter.chat(request, client=client)
    assert inner.calls == 0, "an over-ceiling request was sent anyway"


def test_the_organization_ceiling_is_checked_too():
    adapter, client, inner, _, coordinator = make_adapter(
        [response_with()], quota=QuotaConfig(max_concurrency=2, max_rpm=10, max_tpm=50))
    with pytest.raises(TokenCeilingExceeded):
        adapter.chat({"model": "m", "messages": [{"role": "user", "content": "x" * 400}],
                      "max_tokens": 100}, client=client)
    assert inner.calls == 0


class Unmeasurable:
    """Content no serializer can bound. It must not resolve to "small"."""


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": Unmeasurable()}],
    [Unmeasurable()],
    object(),
])
def test_unknown_token_demand_fails_closed(messages):
    with pytest.raises(UnknownTokenDemand):
        admission_demand(messages, 100)


def test_an_unmeasurable_request_is_never_sent():
    adapter, client, inner, _, coordinator = make_adapter([response_with()])
    with pytest.raises(UnknownTokenDemand):
        adapter.chat({"model": "m", "messages": [{"role": "user", "content": Unmeasurable()}],
                      "max_tokens": 100}, client=client)
    assert inner.calls == 0


def test_a_request_with_no_output_cap_is_refused():
    from backend.provider_authority import MissingOutputCap

    adapter, client, inner, _, coordinator = make_adapter([response_with()])
    with pytest.raises(MissingOutputCap):
        adapter.chat({"model": "m", "messages": MESSAGES}, client=client)
    assert inner.calls == 0


def test_an_authoritative_count_is_preferred_and_a_broken_one_is_not_trusted():
    try:
        register_token_counter(lambda messages, tools: 7)
        demand = admission_demand(MESSAGES, 100)
        assert demand.basis == AUTHORITATIVE_BASIS
        assert demand.input_tokens < conservative_input_tokens(MESSAGES)

        register_token_counter(lambda messages, tools: 1 / 0)
        fallback = admission_demand(MESSAGES, 100)
        assert fallback.basis == CONSERVATIVE_BASIS
        assert fallback.input_tokens == conservative_input_tokens(MESSAGES)

        register_token_counter(lambda messages, tools: -5)
        assert admission_demand(MESSAGES, 100).basis == CONSERVATIVE_BASIS
    finally:
        register_token_counter(None)


# =============================================================================
# 4. Search accounting
# =============================================================================

SEARCH_REQUEST = {
    "model": "kimi-k2.6", "messages": MESSAGES, "max_tokens": 100,
    "tools": [{"type": "builtin_function", "function": {"name": BUILTIN_WEB_SEARCH}}],
}


def test_a_builtin_search_is_counted_in_run_accounting():
    """V1 searches through the builtin `$web_search` tool INSIDE a chat call.
    The provider runs and bills it there, so the chat gate paces it -- but it
    is still a search the run performed, and nothing used to count it."""
    tracker = make_tracker(max_model_calls_per_run=10,
                           search_cost_per_invocation=0.01)
    adapter, client, inner, _, coordinator = make_adapter(
        [response_with(searches=2)], tracker=tracker)
    adapter.chat(SEARCH_REQUEST, client=client)
    ledger = tracker.ledger_snapshot()
    assert ledger["search_invocations"] == 2
    assert ledger["search_cost"] == pytest.approx(0.02)
    # And it is real money, so it reaches the run's recorded cost too.
    assert tracker.actual_cost >= 0.02


def test_offering_search_without_using_it_accounts_no_search():
    tracker = make_tracker(max_model_calls_per_run=10)
    adapter, client, _, _, coordinator = make_adapter([response_with(searches=0)], tracker=tracker)
    adapter.chat(SEARCH_REQUEST, client=client)
    assert tracker.ledger_snapshot()["search_invocations"] == 0


def test_search_invocations_are_bounded_before_the_search_happens():
    """Learning afterwards that a run went over is not a ceiling."""
    tracker = make_tracker(max_model_calls_per_run=50,
                           max_search_invocations_per_run=2)
    adapter, client, inner, _, coordinator = make_adapter(
        [response_with(searches=2), response_with(searches=1)], tracker=tracker)
    adapter.chat(SEARCH_REQUEST, client=client)
    assert tracker.search_invocations == 2
    with pytest.raises(BudgetExceeded) as refused:
        adapter.chat(SEARCH_REQUEST, client=client)
    assert refused.value.code == "SEARCH_LIMIT_REACHED"
    assert inner.calls == 1, "a search request was sent with no allowance left"


def test_a_standalone_search_consumes_qps_and_run_accounting():
    tracker = make_tracker(search_cost_per_invocation=0.02)
    adapter, _, _, _, coordinator = make_adapter([], tracker=tracker, quota=QuotaConfig())
    adapter.search("search")
    assert tracker.search_invocations == 1
    assert tracker.search_cost == pytest.approx(0.02)
    # The endpoint's own QPS bucket was consumed by that admission.
    assert not coordinator.try_admit_search("search")[0]


def test_every_ledger_decision_the_tracker_emits_is_one_the_database_accepts():
    """A guard against the class of bug this file nearly shipped.

    `run_usage_ledger.decision` is a CHECK-constrained vocabulary. A tracker
    that invents a new decision name writes a row the database refuses -- at
    runtime, mid-run, on a paid call. So the names the tracker can emit are
    checked against the migration that defines them.
    """
    import ast
    import inspect
    import re
    from pathlib import Path as _Path

    from backend import budget as budget_module

    emitted = set()
    for node in ast.walk(ast.parse(inspect.getsource(budget_module))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_ledger" and node.args
                and isinstance(node.args[0], ast.Constant)):
            emitted.add(node.args[0].value)
    assert emitted, "no ledger decisions found at all"

    migrations = _Path(__file__).resolve().parents[1] / "supabase" / "migrations"
    allowed: set[str] = set()
    for path in sorted(migrations.glob("*.sql")):
        for match in re.finditer(r"check\s*\(decision in \(([^)]*)\)\)",
                                 path.read_text(encoding="utf-8")):
            allowed = {value.strip().strip("'") for value in match.group(1).split(",")}
    assert allowed, "no decision vocabulary found in the migrations"
    assert emitted <= allowed, f"the tracker emits decisions the database refuses: {emitted - allowed}"


def test_a_search_is_durable_through_the_execution_usage_snapshot():
    """It has no per-call ledger row, so the snapshot is the durable record."""
    recorded: list[dict] = []
    tracker = BudgetTracker(BudgetConfig(estimated_cost_per_call=0.0,
                                         search_cost_per_invocation=0.03),
                            kill_switch=lambda: True,
                            usage_recorder=lambda snapshot: recorded.append(dict(snapshot)))
    tracker.record_search()
    assert recorded, "a search recorded nothing durable at all"
    assert recorded[-1]["search_invocations"] == 1
    assert recorded[-1]["search_cost"] == pytest.approx(0.03)


def test_the_search_bound_and_price_come_from_the_runtime_policy():
    from backend.runtime_policy import BUDGET, dimensions_for, reviewed_first_run_policy

    policy = reviewed_first_run_policy()
    config = policy.budget_config()
    assert config.max_search_invocations_per_run == policy["max_search_invocations_per_run"]
    assert config.search_cost_per_invocation == policy["search_cost_per_invocation"]
    names = {d.name for d in dimensions_for(BUDGET)}
    assert {"max_search_invocations_per_run", "search_cost_per_invocation"} <= names


# =============================================================================
# 5. The deadline is ABSOLUTE, not per sub-operation
# =============================================================================

@pytest.mark.parametrize("deadline", [0.5, 5.0, 30.0, 90.0, 600.0])
def test_the_sub_operation_timeouts_sum_to_no_more_than_the_deadline(deadline):
    """The defect this arithmetic removes: httpx gives pool, connect, write
    and read each their OWN timeout, so five sub-operations each allowed D
    seconds run for up to 5D with every individual timeout honoured."""
    allocation = allocate_request_timeouts(deadline)
    assert set(allocation) == {"pool", "connect", "write", "read"}
    assert sum(allocation.values()) <= deadline + 1e-9
    assert allocation["read"] > 0


def test_the_transport_overrides_a_client_that_would_give_a_phase_the_whole_deadline():
    """The client is built with read=D; the transport must still hand the
    inner transport an ALLOCATION, or the header wait alone can spend the
    whole budget and leave nothing bounded for the rest."""
    import httpx

    seen = {}

    def record(request):
        seen.update(request.extensions.get("timeout") or {})
        return httpx.Response(200, json={})

    with build_deadline_http_client(20.0, inner=httpx.MockTransport(record)) as client:
        client.post("http://provider.invalid/v1/chat", json={})
    assert seen, "the transport passed no timeout allocation at all"
    assert sum(seen.values()) <= 20.0 + 1e-9
    assert seen["read"] < 20.0, "the header wait was given the whole deadline"


class _StallThenTrickle(http.server.BaseHTTPRequestHandler):
    """Silent while "computing", then a response that never stops arriving.

    Both phases at once, which is the case a single instrument cannot bound:
    an inactivity timeout misses the trickle, and a body-only check misses
    the silence.
    """

    protocol_version = "HTTP/1.1"
    stall_seconds = 0.6
    chunk_seconds = 0.05

    def do_POST(self):  # noqa: N802
        time.sleep(self.stall_seconds)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"8\r\n........\r\n")
                self.wfile.flush()
                time.sleep(self.chunk_seconds)
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def stall_then_trickle():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StallThenTrickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/chat"
    finally:
        server.shutdown()
        server.server_close()


def test_the_deadline_covers_the_header_wait_and_the_response_together(stall_then_trickle):
    import httpx

    deadline = 2.0
    started = time.monotonic()
    with build_deadline_http_client(deadline) as client:
        with pytest.raises((ProviderRequestDeadlineExceeded, httpx.TimeoutException)):
            client.post(stall_then_trickle, json={})
    elapsed = time.monotonic() - started
    assert elapsed < deadline + 1.0, (
        f"the request escaped its {deadline}s total deadline: {elapsed:.2f}s")


# =============================================================================
# 6. #102 preserved: an unproven outcome keeps the organization permit
# =============================================================================

@pytest.mark.parametrize("exc,reason", [
    (ProviderRequestDeadlineExceeded(5.0, 5.1), "PROVIDER_REQUEST_DEADLINE_EXCEEDED"),
    (RuntimeError("something nobody named"), "PROVIDER_REQUEST_OUTCOME_UNKNOWN"),
])
def test_an_unproven_outcome_does_not_free_organization_occupancy(exc, reason):
    """MILO stopped waiting is not the provider stopped working."""
    adapter, client, _, _, coordinator = make_adapter(
        [exc], quota=QuotaConfig(max_concurrency=1, max_rpm=10, max_tpm=1_000_000),
        guarded=False)

    verdict = classify_outcome(exc)
    assert not verdict.completion_proven
    assert verdict.unproven_reason == reason

    with pytest.raises(type(exc)):
        adapter.chat(REQUEST, client=client)
    assert coordinator.try_acquire_inference() is None, (
        "an unproven outcome handed the organization permit to the next caller")
    assert adapter.scheduler._slots.acquire(blocking=False), "the local slot leaked"
    adapter.scheduler._slots.release()


def test_a_proven_provider_answer_does_free_the_permit_at_once():
    """The other half: ordinary backpressure must stay fast."""
    adapter, client, _, _, coordinator = make_adapter(
        [status_error("Error code: 429", 429), response_with()],
        quota=QuotaConfig(max_concurrency=1, max_rpm=10, max_tpm=1_000_000),
        guarded=False)
    assert adapter.chat(REQUEST, client=client) is not None
    lease = coordinator.try_acquire_inference()
    assert lease is not None, "a proven 429 stranded the permit"
    lease.release()


# =============================================================================
# 7. V1 and V2 are the SAME authority
# =============================================================================

def test_both_engines_route_through_one_adapter_instance():
    from backend.engines.swarm_v2.model_gateway import ModelGateway
    from backend.engines.vehicle_catalog_v1 import core as v1_core
    from backend.engines.vehicle_catalog_v1.engine import VehicleCatalogEngine

    adapter = ProviderAdapter(ProviderScheduler(ProviderLimitsConfig()))
    engine = VehicleCatalogEngine(provider_adapter=adapter)
    engine._install_injections()
    try:
        gateway = ModelGateway(guarded_client_factory=lambda k, u: ScriptedClient([]),
                               adapter=adapter, api_key="k", base_url="u")
        assert v1_core.PROVIDER_ADAPTER is adapter
        assert v1_core._provider_authority() is adapter
        assert gateway._adapter is adapter
        assert v1_core.PROVIDER_SCHEDULER is adapter.scheduler
    finally:
        engine._restore_injections()


def test_the_worker_builds_exactly_one_adapter_for_both_engines():
    import inspect

    from backend.worker import main as worker_main

    source = inspect.getsource(worker_main)
    assert source.count("ProviderAdapter(") == 1, (
        "the worker builds more than one provider authority")
    assert source.count("ProviderScheduler(") == 1, (
        "the worker builds a second scheduler beside the authority's")
    assert "provider_adapter=provider_adapter" in source
    assert "adapter=provider_adapter" in source


def test_neither_engine_states_a_provider_mechanic_of_its_own():
    import inspect

    from backend.engines.swarm_v2 import model_gateway
    from backend.engines.vehicle_catalog_v1 import core as v1_core

    for module in (v1_core, model_gateway):
        source = inspect.getsource(module)
        assert "chat.completions.create" not in source, (
            f"{module.__name__} makes a raw provider call")
        assert "max_retries=" not in source, (
            f"{module.__name__} configures provider retries")
        assert "estimate_admission_tokens" not in source, (
            f"{module.__name__} computes its own admission value")

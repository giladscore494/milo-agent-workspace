"""PR-S: reasoning-safe transport for every Swarm V2 provider call.

Includes the MANDATORY regression replay of run 5145ca65: Commander planning
on kimi-k3 (effort high, max_completion_tokens 32,000, json_object) sent
NON-streaming under a 90s deadline, which ended ~70s after send with no
provider response object -- the lease quarantined as
PROVIDER_REQUEST_OUTCOME_UNKNOWN, 0 tokens recorded, and the run failed as
COMMANDER_COMPLETION_FAILED.

Everything runs through production code: the real OpenAI SDK, the real
deadline transport, the guarded client, the provider authority, the scheduler
and a real (in-memory) quota coordinator. Only the provider is simulated
(``tests/fakes/streaming_provider.py``), on a simulated clock, so a
160-second reasoning call takes milliseconds. The inactivity test uses a real
loopback socket. No provider is called.
"""
from __future__ import annotations

import http.server
import io
import json
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from backend.budget import BudgetConfig, BudgetTracker, build_guarded_client_factory
from backend.engines.swarm_v2 import (Commander, CommanderModelResolver, PlanLimits,
                                      PlanValidator)
from backend.engines.swarm_v2 import model_gateway
from backend.engines.swarm_v2.model_gateway import (MAX_ROLE_TOTAL_DEADLINE_SECONDS,
                                                    ROLE_POLICIES, ModelGateway)
from backend.engines.swarm_v2.request_builder import build_provider_request
from backend.model_profiles import PROFILES
from backend import provider_streaming
from backend.provider_authority import ProviderAdapter
from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    ProviderQuotaUnavailable, QuotaConfig,
                                    UpstashQuotaBackend, assert_request_deadline_safe,
                                    default_request_deadline)
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.provider_streaming import (PROVIDER_CONNECTION_FAILED,
                                        PROVIDER_REQUEST_DEADLINE_EXCEEDED,
                                        PROVIDER_STREAM_INACTIVITY_TIMEOUT,
                                        PROVIDER_STREAM_INTERRUPTED, STREAM_INACTIVITY_SECONDS,
                                        ProviderStreamInterrupted, ProviderTransportFailure,
                                        assemble_chat_stream, transport_failure_code)
from backend.provider_transport import (ProviderRequestDeadlineExceeded,
                                        allocate_request_timeouts, build_deadline_http_client)
from tests.fakes.streaming_provider import (REASONING_SENTINEL, SimClock, SimulatedKimi,
                                            StreamScript)
from tests.test_swarm_v2_smoke_offline import minimal_plan

PLAN_JSON = json.dumps(minimal_plan(num_tasks=1))
API_KEY = "sk-sim-KEY-SENTINEL"
OBJECTIVE = "OBJECTIVE-SENTINEL find the 2019 model year trims"


# =============================================================================
# the stack: production code end to end, a simulated provider at the bottom
# =============================================================================

class Stack(SimpleNamespace):
    def held(self):
        return self.coordinator.held_inference_leases()

    def quarantines(self):
        return [p for k, p in self.diagnostics if k == "provider_lease_quarantined"]

    def settled(self):
        return [r for r in self.rows if r["decision"] == "settled"]


def build_stack(monkeypatch, scripts, *, status=None, client_deadline=None,
                budget=None, **tracker_kwargs):
    clock = SimClock()
    monkeypatch.setattr(provider_streaming, "_monotonic", clock)
    kimi = SimulatedKimi(clock=clock, scripts=list(scripts), status=status)
    config = QuotaConfig()
    deadline = config.request_deadline_seconds if client_deadline is None else client_deadline
    rows: list[dict] = []
    tracker = BudgetTracker(budget or BudgetConfig(max_model_calls_per_run=50),
                            kill_switch=lambda: True, ledger_recorder=rows.append,
                            **tracker_kwargs)

    def inner_factory(api_key, base_url):
        return OpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                      http_client=build_deadline_http_client(deadline, inner=kimi, clock=clock))

    diagnostics: list[tuple[str, dict]] = []
    coordinator = ProviderQuotaCoordinator(
        MemoryQuotaBackend(), config,
        diagnostic_sink=lambda kind, payload: diagnostics.append((kind, payload)))
    scheduler = ProviderScheduler(
        ProviderLimitsConfig(max_concurrency=1, rpm_limit=None, max_rate_limit_retries=0,
                             max_backpressure_wait_seconds=1, backoff_base_seconds=.001,
                             backoff_max_seconds=.001),
        coordinator=coordinator)
    adapter = ProviderAdapter(scheduler, clock=clock, log_context={"run_id": "run-5145ca65-sim"})
    gateway = ModelGateway(
        guarded_client_factory=build_guarded_client_factory(tracker, inner_factory=inner_factory),
        adapter=adapter, api_key=API_KEY, base_url="https://sim.invalid/v1")
    return Stack(clock=clock, kimi=kimi, tracker=tracker, rows=rows, gateway=gateway,
                 coordinator=coordinator, diagnostics=diagnostics)


def commander(gateway, model="kimi-k3"):
    return Commander(client=gateway, resolver=CommanderModelResolver((model,), {model}),
                     validator=PlanValidator(allowed_tools=set(), limits=PlanLimits(max_tasks=64)))


def plan(stack):
    return commander(stack.gateway).plan(requested_model="kimi-k3", objective=OBJECTIVE,
                                         context={})


def call_logs(capsys):
    out = capsys.readouterr().out
    return out, [json.loads(line) for line in out.splitlines()
                 if line.startswith("{") and '"event":"provider_call"' in line]


def assert_sanitized(text):
    for secret in (REASONING_SENTINEL, API_KEY, OBJECTIVE, "PROVIDER-BODY-SENTINEL",
                   "sim.invalid"):
        assert secret not in text, f"{secret!r} leaked"


# =============================================================================
# 1. every Swarm V2 request is streamed
# =============================================================================

@pytest.mark.parametrize("model", ["kimi-k3", "kimi-k2.6"])
@pytest.mark.parametrize("role", sorted(ROLE_POLICIES))
def test_every_swarm_v2_request_streams_without_undocumented_stream_options(model, role):
    request = build_provider_request(PROFILES[model], ROLE_POLICIES[role],
                                     [{"role": "user", "content": "x"}], None)
    assert request["stream"] is True
    # Not documented for the Kimi models in any source MILO could verify, so
    # never sent: usage comes from the final choice (or a usage chunk).
    assert "stream_options" not in request


def test_every_role_declares_its_total_deadline():
    deadlines = {role: policy.total_deadline_seconds for role, policy in ROLE_POLICIES.items()}
    assert deadlines == {("commander", "planning"): 600.0, ("commander", "replanning"): 300.0,
                         ("verifier", "verification"): 480.0, ("worker", "execute"): 300.0}
    assert MAX_ROLE_TOTAL_DEADLINE_SECONDS == 600.0


# =============================================================================
# 2. slow reasoning, well past the old 90s total, completes
# =============================================================================

def test_slow_reasoning_stream_past_the_old_90s_total_completes_and_releases_the_lease(
        monkeypatch, capsys):
    """40 reasoning chunks, 4 simulated seconds apart: 160s of thinking, then
    the answer. Under the old transport that was 67.5s of silence allowed."""
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON)])
    started = stack.clock()

    result = plan(stack)

    assert result.graph.tasks[0].task_id == "t0"
    elapsed = stack.clock() - started
    assert elapsed > 160.0, "the call really did outlive the old 90s deadline"
    (wire,) = stack.kimi.requests
    assert wire["stream"] is True and "stream_options" not in wire
    assert wire["reasoning_effort"] == "high" and wire["max_completion_tokens"] == 32_000
    # The inactivity window, not the whole budget, bounds every read.
    assert stack.kimi.timeouts[0]["read"] == STREAM_INACTIVITY_SECONDS
    # PROVEN finished: the stream ended with a finish_reason -> lease released.
    assert stack.held() == [] and stack.quarantines() == []
    # Final-chunk usage reached the ledger, reasoning included.
    (row,) = stack.settled()
    assert row["actual_input_tokens"] == 7_000 and row["actual_output_tokens"] == 21_000
    assert row["reasoning_tokens"] == 20_000
    assert stack.kimi.streams[0].closed
    out, (record,) = call_logs(capsys)
    assert record["role"] == "commander:planning" and record["model"] == "kimi-k3"
    assert record["effort"] == "high" and record["cap"] == 32_000 and record["stream"] is True
    assert record["time_to_first_chunk_ms"] == 4_000
    assert record["total_ms"] >= 160_000
    assert record["finish_reason"] == "stop" and record["outcome"] == "success"
    assert record["completion_proven"] is True and record["code"] is None
    assert (record["prompt_tokens"], record["completion_tokens"],
            record["reasoning_tokens"]) == (7_000, 21_000, 20_000)
    assert record["total_deadline_s"] == 600.0 and record["run_id"] == "run-5145ca65-sim"
    durable = json.dumps([stack.rows, stack.tracker.ledger_snapshot(),
                          result.model_dump(mode="json")], default=str)
    assert_sanitized(durable + out)


def test_reasoning_is_never_part_of_the_assembled_completion():
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": REASONING_SENTINEL},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "{\"a\":"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "1}",
                                            "reasoning_content": REASONING_SENTINEL},
                      "finish_reason": "stop"}]},
    ]
    assembled = assemble_chat_stream(iter(chunks))
    assert assembled.choices[0].message.content == "{\"a\":1}"
    assert assembled.choices[0].finish_reason == "stop"
    assert REASONING_SENTINEL not in repr(assembled)
    assert not hasattr(assembled.choices[0].message, "reasoning_content")


def test_usage_on_a_trailing_usage_only_chunk_is_read(monkeypatch):
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON, usage_style="chunk")])
    plan(stack)
    (row,) = stack.settled()
    assert row["actual_input_tokens"] == 7_000 and row["actual_output_tokens"] == 21_000
    assert stack.held() == []


def test_a_finished_stream_without_usage_is_charged_its_whole_reservation(monkeypatch):
    """Unmeasured spend is not an absence of spend."""
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON, usage_style=None)])
    plan(stack)
    (row,) = stack.settled()
    assert row["actual_output_tokens"] == 32_000
    assert row["actual_input_tokens"] > 0
    assert row["actual_cost"] > 0


# =============================================================================
# 3. transport outcomes: bounded, named, and never proof of completion
# =============================================================================

def test_the_total_deadline_stops_a_stream_that_never_finishes(monkeypatch, capsys):
    """200 chunks x 4s = 800s of reasoning against planning's 600s deadline."""
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON, reasoning_chunks=200)])
    started = stack.clock()
    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)
    assert caught.value.code == PROVIDER_REQUEST_DEADLINE_EXCEEDED
    assert caught.value.role == "commander:planning"
    assert 600.0 <= stack.clock() - started <= 604.0
    # MILO stopped waiting; the provider may not have. The slot stays held.
    assert len(stack.held()) == 1
    assert [q["reason"] for q in stack.quarantines()] == ["PROVIDER_REQUEST_DEADLINE_EXCEEDED"]
    released = [r for r in stack.rows if r["decision"] == "settled"]
    # Unknown after send: charged the whole reservation, never zero.
    assert released[0]["actual_output_tokens"] == 32_000
    out, (record,) = call_logs(capsys)
    assert record["code"] == PROVIDER_REQUEST_DEADLINE_EXCEEDED
    assert record["exception_class"] == "ProviderRequestDeadlineExceeded"
    assert record["completion_proven"] is False and record["outcome"] == "timeout"
    assert_sanitized(out + str(caught.value))


def test_a_dropped_stream_stays_outcome_unknown(monkeypatch, capsys):
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON, drop_after=10)])
    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)
    assert caught.value.code == PROVIDER_STREAM_INTERRUPTED
    assert len(stack.held()) == 1
    assert [q["reason"] for q in stack.quarantines()] == ["PROVIDER_REQUEST_OUTCOME_UNKNOWN"]
    out, (record,) = call_logs(capsys)
    assert record["code"] == PROVIDER_STREAM_INTERRUPTED
    assert record["exception_class"] == "APIConnectionError"
    assert record["cause_class"] == "RemoteProtocolError"
    assert record["chunk_count"] == 10 and record["time_to_first_chunk_ms"] == 4_000
    assert_sanitized(out)


def test_a_stream_that_ends_without_a_finish_reason_is_not_proof(monkeypatch):
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON, finish_reason=None)])
    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)
    assert caught.value.code == PROVIDER_STREAM_INTERRUPTED
    assert [q["reason"] for q in stack.quarantines()] == ["PROVIDER_REQUEST_OUTCOME_UNKNOWN"]


def test_an_http_error_is_named_by_status_only_and_proves_completion(monkeypatch, capsys):
    stack = build_stack(monkeypatch, [], status=500)
    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)
    assert caught.value.code == "PROVIDER_HTTP_500"
    # A provider response object is proof the exchange is over.
    assert stack.held() == [] and stack.quarantines() == []
    out, (record,) = call_logs(capsys)
    assert record["code"] == "PROVIDER_HTTP_500"
    assert_sanitized(out + str(caught.value) + repr(caught.value))


class _SilentAfterHeaders(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    silence = 5.0

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = ('data: {"id":"c","object":"chat.completion.chunk","created":1,'
                 '"model":"kimi-k3","choices":[{"index":0,"delta":{"reasoning_content":"t"},'
                 '"finish_reason":null}]}\n\n').encode()
        try:
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            time.sleep(self.silence)
        except Exception:  # noqa: BLE001 - the client hanging up is the point
            pass

    def log_message(self, *args):
        pass


def test_a_silent_stream_trips_the_inactivity_timeout_on_a_real_socket(monkeypatch):
    """Real loopback, real clock: one chunk, then silence. The stream's
    inactivity window (shortened for the test) ends it, not the total."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SilentAfterHeaders)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(model_gateway, "STREAM_INACTIVITY_SECONDS", 0.4)
        rows: list[dict] = []
        tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=5),
                                kill_switch=lambda: True, ledger_recorder=rows.append)
        diagnostics: list = []
        coordinator = ProviderQuotaCoordinator(
            MemoryQuotaBackend(), QuotaConfig(),
            diagnostic_sink=lambda kind, payload: diagnostics.append((kind, payload)))
        scheduler = ProviderScheduler(ProviderLimitsConfig(
            max_concurrency=1, rpm_limit=None, max_rate_limit_retries=0,
            max_backpressure_wait_seconds=1, backoff_base_seconds=.001,
            backoff_max_seconds=.001), coordinator=coordinator)
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        gateway = ModelGateway(
            guarded_client_factory=build_guarded_client_factory(
                tracker, request_deadline_seconds=QuotaConfig().request_deadline_seconds),
            adapter=ProviderAdapter(scheduler), api_key="sk-local", base_url=base_url)
        started = time.monotonic()
        with pytest.raises(ProviderTransportFailure) as caught:
            commander(gateway).plan(requested_model="kimi-k3", objective="o", context={})
        elapsed = time.monotonic() - started
        assert caught.value.code == PROVIDER_STREAM_INACTIVITY_TIMEOUT
        assert elapsed < 3.0, f"the silent stream held the call for {elapsed:.1f}s"
        assert [p["reason"] for k, p in diagnostics
                if k == "provider_lease_quarantined"] == ["PROVIDER_REQUEST_OUTCOME_UNKNOWN"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("exc,code", [
    (ProviderRequestDeadlineExceeded(600, 601), PROVIDER_REQUEST_DEADLINE_EXCEEDED),
    (ProviderStreamInterrupted(), PROVIDER_STREAM_INTERRUPTED),
    (httpx.ReadTimeout("t"), PROVIDER_STREAM_INACTIVITY_TIMEOUT),
    (httpx.RemoteProtocolError("r"), PROVIDER_STREAM_INTERRUPTED),
    (httpx.ConnectError("c"), PROVIDER_CONNECTION_FAILED),
    (httpx.ConnectTimeout("c"), PROVIDER_CONNECTION_FAILED),
    (SimpleNamespace(), None),
    (RuntimeError("anything"), None),
    (ValueError("programming error"), None),
])
def test_transport_failure_codes_are_static(exc, code):
    if isinstance(exc, SimpleNamespace):
        exc = None
    assert transport_failure_code(exc) == code


def test_a_wrapped_status_error_is_named_by_its_status():
    request = httpx.Request("POST", "https://sim.invalid/v1/chat/completions")
    response = httpx.Response(503, request=request, json={"error": {"message": "secret"}})

    class APIStatusError(Exception):
        def __init__(self):
            super().__init__("secret body")
            self.response = response
            self.status_code = 503

    try:
        try:
            raise APIStatusError()
        except APIStatusError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert transport_failure_code(outer) == "PROVIDER_HTTP_503"


def test_scheduler_verdicts_keep_their_own_codes(monkeypatch):
    """ProviderBackpressureExceeded chains the 429 behind it; it must still be
    PROVIDER_BACKPRESSURE_EXCEEDED to the worker, not PROVIDER_HTTP_429."""
    from backend.provider_scheduler import ProviderBackpressureExceeded

    class Adapter:
        def chat(self, *args, **kwargs):
            raise ProviderBackpressureExceeded("x")

    gateway = ModelGateway(guarded_client_factory=lambda *_: None, adapter=Adapter(),
                           api_key="k", base_url="u")
    with pytest.raises(ProviderBackpressureExceeded):
        gateway.call(model="kimi-k3", agent="worker:t0", phase="execute",
                     messages=[{"role": "user", "content": "x"}])


def test_a_worker_task_fails_with_the_transport_code(monkeypatch):
    from backend.engines.swarm_v2.worker import GenericWorker
    from backend.tools import ToolContext, ToolRegistry
    from tests.test_reasoning_truncation import task

    stack = build_stack(monkeypatch, [StreamScript(answer="{}", drop_after=3)])
    result = GenericWorker(gateway=stack.gateway, tools=ToolRegistry(), model="kimi-k3",
                           tool_context=ToolContext()).execute(task(), {})
    assert result.status == "failed"
    assert result.error["code"] == PROVIDER_STREAM_INTERRUPTED


# =============================================================================
# 4. the regression replay of run 5145ca65
# =============================================================================

def test_replay_5145ca65_the_arithmetic_of_the_old_failure():
    """Why it died at ~70s under a 90s deadline: the silent header wait was
    allocated what the setup phases left, 90 - (4.5 + 9 + 9) = 67.5s."""
    old = QuotaConfig(lease_ttl_seconds=120.0)
    assert old.request_deadline_seconds == 90.0
    assert allocate_request_timeouts(old.request_deadline_seconds)["read"] == 67.5
    # Now: planning's streamed call has 600s in total and 60s between chunks.
    new = allocate_request_timeouts(600.0, None, STREAM_INACTIVITY_SECONDS)
    assert new["read"] == 60.0
    assert sum(new.values()) <= 600.0


def _pre_prs_call_path(monkeypatch):
    """The call path as run 5145ca65 had it: non-streaming, no role timing."""
    original = model_gateway.build_provider_request

    def non_streaming(*args, **kwargs):
        request = original(*args, **kwargs)
        request.pop("stream")
        return request

    monkeypatch.setattr(model_gateway, "build_provider_request", non_streaming)
    monkeypatch.setattr(model_gateway, "request_timing", lambda *a, **k: nullcontext())


def test_replay_5145ca65_as_it_happened_and_now_named(monkeypatch, capsys):
    """The run as it happened: kimi-k3 planning, effort high, cap 32,000,
    json_object, NON-streaming, 90s deadline, a provider thinking for 150s.

    Unchanged: the request is UNKNOWN and the lease stays quarantined.
    Changed: the run is no longer told COMMANDER_COMPLETION_FAILED, and the
    call is charged its whole worst-case reservation instead of 0 tokens."""
    _pre_prs_call_path(monkeypatch)
    script = StreamScript(answer=PLAN_JSON, reasoning_chunks=30, reasoning_interval=5.0)
    stack = build_stack(monkeypatch, [script], client_deadline=90.0)
    started = stack.clock()

    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)

    (wire,) = stack.kimi.requests
    assert "stream" not in wire and wire["response_format"] == {"type": "json_object"}
    assert wire["max_completion_tokens"] == 32_000 and wire["reasoning_effort"] == "high"
    # ~70s after send: the 67.5s header read timeout, no response object.
    assert stack.clock() - started == pytest.approx(67.5)
    assert [q["reason"] for q in stack.quarantines()] == ["PROVIDER_REQUEST_OUTCOME_UNKNOWN"]
    assert len(stack.held()) == 1
    (row,) = stack.settled()
    assert row["actual_input_tokens"] > 0 and row["actual_output_tokens"] == 32_000
    assert row["actual_cost"] > 0
    # ...and now it is named for what it was.
    assert caught.value.code == PROVIDER_STREAM_INACTIVITY_TIMEOUT
    assert caught.value.code != "COMMANDER_COMPLETION_FAILED"
    out, (record,) = call_logs(capsys)
    assert record["exception_class"] == "APITimeoutError"
    assert record["cause_class"] == "ReadTimeout"
    assert record["stream"] is False and record["total_ms"] == 67_500
    assert_sanitized(out)


def test_replay_5145ca65_the_same_provider_streamed_now_completes(monkeypatch):
    """The same 150s of reasoning, sent the PR-S way."""
    script = StreamScript(answer=PLAN_JSON, reasoning_chunks=30, reasoning_interval=5.0)
    stack = build_stack(monkeypatch, [script])
    result = plan(stack)
    assert result.graph.tasks[0].task_id == "t0"
    assert stack.held() == [] and stack.quarantines() == []
    (row,) = stack.settled()
    assert row["actual_output_tokens"] == 21_000


def test_replay_5145ca65_reaches_run_error_and_the_terminal_event(monkeypatch):
    from backend.worker.main import execute_run
    from tests.test_worker import WorkerRepo

    _pre_prs_call_path(monkeypatch)
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON)], client_deadline=90.0)

    class Swarm:
        workflow_key = "swarm_v2"

        def run(self, run):
            plan(stack)

    class SwarmRepo(WorkerRepo):
        def get_project(self, project_id):
            return {"id": project_id, "workflow_key": "swarm_v2"}

    repo = SwarmRepo()
    assert execute_run(repo.run_id, repo, Swarm()) == 0
    assert repo.failed == (repo.run_id, PROVIDER_STREAM_INACTIVITY_TIMEOUT,
                           "the provider stream went silent past the inactivity timeout")
    run_id, kind, event = repo.events[-1]
    assert kind == "run_failed"
    assert event["payload"]["code"] == PROVIDER_STREAM_INACTIVITY_TIMEOUT
    assert event["payload"]["role"] == "commander:planning"
    assert_sanitized(json.dumps(repo.events, default=str))


# =============================================================================
# 5. the lease window admits the longest role
# =============================================================================

def test_the_reviewed_lease_window_admits_the_longest_role_deadline():
    config = QuotaConfig()
    assert config.lease_ttl_seconds == 800.0
    assert default_request_deadline(config.lease_ttl_seconds) == 600.0
    assert_request_deadline_safe(MAX_ROLE_TOTAL_DEADLINE_SECONDS, config.lease_ttl_seconds)
    # V1 and standalone search keep the deadline they had.
    assert config.non_streaming_request_deadline_seconds == 90.0


def test_a_shorter_deployed_window_clamps_a_role_and_never_widens(monkeypatch):
    """A role deadline can only TIGHTEN the client's ceiling."""
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON, reasoning_chunks=200)],
                        client_deadline=90.0)
    started = stack.clock()
    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)
    assert caught.value.code == PROVIDER_REQUEST_DEADLINE_EXCEEDED
    assert stack.clock() - started <= 94.0


def test_the_run_duration_cap_leaves_room_for_the_longest_call():
    from backend.runtime_policy import (DIMENSIONS, WORKER_JOB_TIMEOUT_SECONDS,
                                        _invariant_violations, longest_provider_call_seconds,
                                        reviewed_policy_violations)

    assert longest_provider_call_seconds() == 660.0
    reviewed = DIMENSIONS["max_run_duration_seconds"].reviewed
    assert reviewed == 1800
    assert reviewed + longest_provider_call_seconds() < WORKER_JOB_TIMEOUT_SECONDS
    assert reviewed_policy_violations() == []
    refused = _invariant_violations({"max_run_duration_seconds": 3000})
    assert [v.dimension for v in refused] == ["max_run_duration_seconds"]
    assert _invariant_violations({"max_run_duration_seconds": 1800}) == []


# =============================================================================
# 6. the ownership probe: why it failed, and the script that failed
# =============================================================================

def test_the_probe_script_never_parses_an_infinite_score_in_lua():
    """A held lease's score is +inf, which ZSCORE returns as the string "inf".
    The old script ran `tonumber(score)` on it; the new one lets Redis compare
    scores itself, like `_LUA_HELD` already did."""
    from backend import provider_quota

    script = provider_quota._LUA_VERIFY
    assert "tonumber" not in script and "ZSCORE" not in script
    assert "ZRANGEBYSCORE" in script and "'(' .. ARGV[2]" in script and "'+inf'" in script


def test_a_store_script_error_is_classified_without_its_text():
    backend = UpstashQuotaBackend(
        "https://example.invalid", "unused",
        http_post=lambda _url, _body: {"error": "ERR user_script:3: attempt to compare nil with number"})
    with pytest.raises(ProviderQuotaUnavailable) as caught:
        backend.verify_concurrency("k", "lease", 1)
    assert caught.value.failure_kind == "store_error_response"
    assert "compare nil" not in str(caught.value)


def test_a_failed_probe_says_why_with_class_names_only(capsys):
    class Store(MemoryQuotaBackend):
        def verify_concurrency(self, key, lease_id, now_ms):
            unavailable = ProviderQuotaUnavailable()
            unavailable.failure_kind = "store_error_response"
            raise unavailable

    events: list = []
    coordinator = ProviderQuotaCoordinator(Store(), QuotaConfig(lease_ttl_seconds=2.0),
                                           diagnostic_sink=lambda k, p: events.append((k, p)))
    scheduler = ProviderScheduler(ProviderLimitsConfig(
        max_concurrency=1, rpm_limit=None, max_rate_limit_retries=0,
        max_backpressure_wait_seconds=1, backoff_base_seconds=.001, backoff_max_seconds=.001),
        coordinator=coordinator)

    def slow_call():
        time.sleep(1.4)   # one probe interval (2s / 4, floored at 1s)
        return "ok"

    assert scheduler.execute(slow_call, agent="commander", phase="planning") == "ok"
    (lost,) = [p for k, p in events if k == "provider_lease_ownership_lost"]
    assert lost["reason"] == "PROVIDER_LEASE_PROBE_FAILED"
    assert lost["exception_class"] == "ProviderQuotaUnavailable"
    assert lost["failure_kind"] == "store_error_response"
    log = [json.loads(line) for line in capsys.readouterr().out.splitlines()
           if '"event":"provider_lease_probe"' in line]
    assert log and log[0]["exception_class"] == "ProviderQuotaUnavailable"
    assert log[0]["role"] == "commander:planning"
    # Observability only: the call still succeeded and settled normally.
    assert coordinator.held_inference_leases() == []


def test_the_log_writer_never_raises():
    class Broken(io.StringIO):
        def write(self, *_):
            raise OSError("sink gone")

    provider_streaming.emit_structured_log({"a": 1}, stream=Broken())
    record = provider_streaming.log_provider_call(
        payload={"model": "kimi-k3"}, agent="worker:secret-task-id", phase="execute",
        started_at=0.0, ended_at=1.0, response=object(), stream=io.StringIO())
    assert record["role"] == "worker:execute"


# =============================================================================
# 7. PR #137 review fixes
# =============================================================================

def _daily_budget_stack(monkeypatch, scripts, **kwargs):
    """The stack with a daily budget, so the daily settlement is observable."""
    settlements: list[tuple] = []
    stack = build_stack(
        monkeypatch, scripts,
        budget=BudgetConfig(max_model_calls_per_run=50, daily_user_budget=100.0),
        daily_user_reserver=lambda amount, seq: f"reservation-{seq}",
        daily_user_cost_provider=lambda: 0.0,
        daily_settler=lambda reservation, cost, status, reason: settlements.append(
            (reservation.id, cost, status, reason)),
        **kwargs)
    stack.settlements = settlements
    return stack


@pytest.mark.parametrize("script, code, reason", [
    (StreamScript(answer=PLAN_JSON, drop_after=10),
     PROVIDER_STREAM_INTERRUPTED, "PROVIDER_OUTCOME_UNKNOWN"),
    (StreamScript(answer=PLAN_JSON, finish_reason=None),
     PROVIDER_STREAM_INTERRUPTED, "PROVIDER_OUTCOME_UNKNOWN"),
    (StreamScript(answer=PLAN_JSON, reasoning_chunks=200),
     PROVIDER_REQUEST_DEADLINE_EXCEEDED, "PROVIDER_DEADLINE_EXCEEDED"),
], ids=["mid-stream-drop", "no-finish-reason", "total-deadline"])
def test_an_unknown_outcome_after_send_is_charged_its_whole_worst_case_reservation(
        monkeypatch, script, code, reason):
    """MONEY: a request that was sent and cannot be proven finished may have
    been billed in full. It used to settle 0 tokens / $0 into the run budget
    and the daily settlement."""
    stack = _daily_budget_stack(monkeypatch, [script])
    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)
    assert caught.value.code == code
    assert len(stack.held()) == 1, "still quarantined: the charge changes no lease rule"

    (reserved,) = [r for r in stack.rows if r["decision"] == "reserved"]
    (row,) = stack.settled()
    worst_case = reserved["estimated_cost"]
    assert worst_case > 0
    # Every reserved input token and the full output cap, at the worst case.
    assert row["actual_output_tokens"] == 32_000
    assert row["actual_input_tokens"] > 0
    assert row["actual_cost"] == pytest.approx(worst_case)
    assert row["actual_cost"] == pytest.approx(float(PROFILES["kimi-k3"].worst_case_cost(
        row["actual_input_tokens"], 32_000)))
    # Nothing was measured: the reasoning and answer counts are null, not 0.
    for name in ("reasoning_tokens", "answer_tokens", "reasoning_tokens_estimated",
                 "cached_input_tokens", "cache_write_tokens"):
        assert row[name] is None, name
    # The run budget...
    tracker = stack.tracker
    assert tracker.output_tokens == 32_000
    assert tracker.input_tokens == row["actual_input_tokens"]
    assert tracker.actual_cost == pytest.approx(worst_case)
    assert tracker.reserved_cost == 0 and tracker.reserved_output_tokens == 0
    assert tracker.reasoning_tokens == 0 and tracker.reasoning_estimated_calls == 0
    assert tracker.provider_failures == 1
    # ...and the daily settlement are both charged the worst case.
    assert stack.settlements == [("reservation-1", pytest.approx(worst_case), "released", reason)]


def test_the_5145ca65_inactivity_timeout_is_charged_its_worst_case_too(monkeypatch):
    """The header/inactivity read timeout of the replay (no response object)."""
    _pre_prs_call_path(monkeypatch)
    script = StreamScript(answer=PLAN_JSON, reasoning_chunks=30, reasoning_interval=5.0)
    stack = _daily_budget_stack(monkeypatch, [script], client_deadline=90.0)
    with pytest.raises(ProviderTransportFailure) as caught:
        plan(stack)
    assert caught.value.code == PROVIDER_STREAM_INACTIVITY_TIMEOUT
    (reserved,) = [r for r in stack.rows if r["decision"] == "reserved"]
    (row,) = stack.settled()
    assert row["actual_output_tokens"] == 32_000
    assert row["actual_cost"] == pytest.approx(reserved["estimated_cost"])
    assert row["reasoning_tokens"] is None and row["answer_tokens"] is None
    assert stack.settlements == [("reservation-1", pytest.approx(reserved["estimated_cost"]),
                                  "released", "PROVIDER_OUTCOME_UNKNOWN")]


def test_a_proven_provider_failure_is_still_charged_nothing(monkeypatch):
    """An HTTP error carries a provider response: the exchange is over and
    nothing was generated, so the worst-case rule does not apply."""
    stack = _daily_budget_stack(monkeypatch, [], status=500)
    with pytest.raises(ProviderTransportFailure):
        plan(stack)
    (row,) = stack.settled()
    assert row["actual_input_tokens"] == 0 and row["actual_output_tokens"] == 0
    assert row["actual_cost"] is None
    assert stack.tracker.actual_cost == 0
    assert stack.settlements == [("reservation-1", 0.0, "released", "PROVIDER_EXCEPTION")]


def _moonshot_chunks(model="kimi-k3"):
    """Real `openai` ChatCompletionChunk objects in Moonshot's shape: usage on
    the FINAL CHOICE (`choices[0].usage`), no usage-only chunk."""
    from openai.types.chat import ChatCompletionChunk

    def chunk(choice):
        return ChatCompletionChunk.model_validate({
            "id": "chatcmpl-moonshot", "object": "chat.completion.chunk", "created": 1,
            "model": model, "choices": [choice]})

    return [
        chunk({"index": 0, "delta": {"role": "assistant", "content": None,
                                     "reasoning_content": REASONING_SENTINEL},
               "finish_reason": None}),
        chunk({"index": 0, "delta": {"content": PLAN_JSON[:10]}, "finish_reason": None}),
        chunk({"index": 0, "delta": {"content": PLAN_JSON[10:]}, "finish_reason": None}),
        chunk({"index": 0, "delta": {}, "finish_reason": "stop",
               "usage": {"prompt_tokens": 7_000, "completion_tokens": 21_000,
                         "total_tokens": 28_000, "cached_tokens": 1_000,
                         "completion_tokens_details": {"reasoning_tokens": 20_000}}}),
    ]


def test_moonshot_choice_usage_on_real_sdk_chunk_objects_is_assembled():
    chunks = _moonshot_chunks()
    # The SDK keeps the undeclared `usage` key on the choice.
    assert chunks[-1].choices[0].usage is not None and chunks[-1].usage is None
    assembled = assemble_chat_stream(iter(chunks))
    assert assembled.choices[0].message.content == PLAN_JSON
    assert assembled.choices[0].finish_reason == "stop"
    counts = provider_streaming.usage_counts(assembled.usage)
    assert counts == {"prompt_tokens": 7_000, "completion_tokens": 21_000,
                      "reasoning_tokens": 20_000, "cached_tokens": 1_000}
    assert REASONING_SENTINEL not in repr(assembled)


def test_moonshot_choice_usage_reaches_the_ledger_without_stream_options():
    sent: list[dict] = []

    class MoonshotCompletions:
        def create(self, **kwargs):
            sent.append(kwargs)
            return iter(_moonshot_chunks())

    rows: list[dict] = []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=5), kill_switch=lambda: True,
                            ledger_recorder=rows.append)
    client = build_guarded_client_factory(tracker, inner_factory=lambda key, url: SimpleNamespace(
        chat=SimpleNamespace(completions=MoonshotCompletions())))("k", "https://sim.invalid/v1")
    request = build_provider_request(PROFILES["kimi-k3"], ROLE_POLICIES[("commander", "planning")],
                                     [{"role": "user", "content": "x"}], None)
    response = client.chat.completions.create(**request)

    (wire,) = sent
    assert wire["stream"] is True and "stream_options" not in wire
    assert response.choices[0].message.content == PLAN_JSON
    (row,) = [r for r in rows if r["decision"] == "settled"]
    assert row["actual_input_tokens"] == 7_000 and row["actual_output_tokens"] == 21_000
    assert row["reasoning_tokens"] == 20_000 and row["cached_input_tokens"] == 1_000


@pytest.mark.parametrize("usage_style", ["choice", "chunk"])
def test_reading_stops_once_finish_reason_and_usage_are_both_seen(monkeypatch, usage_style):
    """A provider that goes silent after its final frame instead of sending
    [DONE] must not turn a finished, measured request into an inactivity
    timeout that quarantines the slot."""
    stack = build_stack(monkeypatch, [StreamScript(answer=PLAN_JSON, usage_style=usage_style,
                                                   hang_after_final=True)])
    result = plan(stack)
    assert result.graph.tasks[0].task_id == "t0"
    (stream,) = stack.kimi.streams
    assert not stream.read_past_final, "the reader waited past a finished, measured stream"
    assert stream.closed
    assert stack.held() == [] and stack.quarantines() == []
    (row,) = stack.settled()
    assert row["actual_input_tokens"] == 7_000 and row["actual_output_tokens"] == 21_000


def test_usage_seen_only_before_the_finish_reason_does_not_end_the_read():
    """Usage that precedes the finish_reason may be partial: keep reading for
    the final one rather than settle on it."""
    def partial_then_final():
        yield {"choices": [{"index": 0, "delta": {"content": "{}"}, "finish_reason": None}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        raise AssertionError("read past the final usage")

    assembled = assemble_chat_stream(partial_then_final())
    assert assembled.usage == {"prompt_tokens": 10, "completion_tokens": 20}


# --- a deployed deadline below the role policy is refused at boot -----------

@pytest.mark.parametrize("override", [
    {"MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS": "300"},
    {"MILO_PROVIDER_LEASE_TTL_SECONDS": "400"},
], ids=["request-timeout", "lease-ttl"])
def test_paid_swarm_v2_refuses_a_deadline_below_the_longest_role(monkeypatch, override):
    """The transport would silently clamp Commander planning's 600s to the
    client ceiling. A paid Swarm V2 run refuses instead, before any call."""
    from uuid import UUID

    from backend.provider_quota import QuotaConfig as _QuotaConfig
    from tests import test_swarm_v2_smoke_offline as smoke

    smoke.swarm_env(monkeypatch, **override)
    assert _QuotaConfig.from_env().request_deadline_seconds < MAX_ROLE_TOTAL_DEADLINE_SECONDS
    repo, conversation_id = smoke.build_repo()
    completions = smoke.FakeKimiCompletions()
    run_id = smoke.run_worker_directly(repo, conversation_id, monkeypatch, completions)
    from backend.worker import main as worker_main

    assert worker_main.execute_run(UUID(str(run_id)), repo) == 0
    run = repo.get_run(run_id)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "PROVIDER_DEADLINE_BELOW_ROLE_POLICY"
    assert completions.calls == [], "a provider call was made under a clamped deadline"


def test_the_reviewed_deadline_admits_the_longest_role_at_boot(monkeypatch):
    from backend.provider_quota import QuotaConfig as _QuotaConfig
    from tests import test_swarm_v2_smoke_offline as smoke

    smoke.swarm_env(monkeypatch, MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS=None,
                    MILO_PROVIDER_LEASE_TTL_SECONDS=None)
    assert _QuotaConfig.from_env().request_deadline_seconds >= MAX_ROLE_TOTAL_DEADLINE_SECONDS
    repo, conversation_id = smoke.build_repo()
    completions = smoke.FakeKimiCompletions()
    run_id = smoke.run_worker_directly(repo, conversation_id, monkeypatch, completions)
    from backend.worker import main as worker_main

    assert worker_main.execute_run(run_id, repo) == 0
    error = repo.get_run(run_id).get("error") or {}
    assert error.get("code") != "PROVIDER_DEADLINE_BELOW_ROLE_POLICY"
    assert completions.calls, "the reviewed configuration must reach the provider"

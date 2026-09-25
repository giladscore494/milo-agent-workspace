"""PR-R commit 2: reasoning-aware usage accounting (counts only)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetTracker, build_guarded_client_factory
from backend.model_profiles import CACHE_TTL_1H, PROFILES
from backend.model_usage import USAGE_REASONING_FIELD_ABSENT, count_content_tokens, read_usage


def response(content, *, prompt=1000, completion=500, cached=None, write=None,
             reasoning=None, reasoning_content="SECRET-CHAIN-OF-THOUGHT", ttl=None):
    details = {}
    if cached is not None:
        details["cached_tokens"] = cached
    if write is not None:
        details["cache_write_tokens"] = write
    if ttl is not None:
        details["cache_write_ttl"] = ttl
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            prompt_tokens_details=details or None,
                            completion_tokens_details=(None if reasoning is None
                                                       else {"reasoning_tokens": reasoning}))
    message = SimpleNamespace(content=content, reasoning_content=reasoning_content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           usage=usage)


def test_reported_fields_are_read_and_absent_ones_stay_none():
    parsed = read_usage(response('{"a":1}', cached=600, write=100, reasoning=420))
    assert (parsed.input_tokens, parsed.output_tokens) == (1000, 500)
    assert (parsed.cached_input_tokens, parsed.cache_write_tokens) == (600, 100)
    assert parsed.reasoning_tokens == 420 and parsed.reasoning_estimated is False
    assert parsed.reasoning_tokens_estimated is None

    bare = read_usage(response('{"a":1}'))
    assert bare.cached_input_tokens is None and bare.cache_write_tokens is None
    assert bare.reasoning_tokens is None and bare.reasoning_estimated is True
    # completion_tokens - answer_tokens, recorded as an ESTIMATE only.
    assert bare.reasoning_tokens_estimated == 500 - bare.answer_tokens
    fields = bare.ledger_fields()
    assert fields["reasoning_tokens"] is None and fields["cached_input_tokens"] is None


def test_moonshot_top_level_cached_tokens_is_accepted():
    r = response("x")
    r.usage.cached_tokens = 12
    assert read_usage(r).cached_input_tokens == 12


def test_answer_tokens_count_content_only_never_reasoning_content():
    r = response("short answer", reasoning_content="x " * 10_000, completion=900)
    parsed = read_usage(r)
    assert parsed.answer_tokens == count_content_tokens("short answer")[0]
    assert parsed.answer_tokens < 10
    # Empty answer (the 4761a8ce shape): every output token is reasoning.
    empty = read_usage(response("", completion=4000))
    assert empty.answer_tokens == 0 and empty.reasoning_tokens_estimated == 4000


def test_pricing_cached_miss_write_and_reasoning_in_output():
    k3 = PROFILES["kimi-k3"]
    # 1M prompt: 600k hit, 100k written (5m), 300k miss; 1M output incl. reasoning.
    cost = k3.usage_cost(input_tokens=1_000_000, output_tokens=1_000_000,
                         cached_input_tokens=600_000, cache_write_tokens=100_000)
    assert cost == Decimal("0.18") + Decimal("0.30") + Decimal("0.90") + Decimal("15.00")
    # The 1-hour tier is dearer, and only applies when reported.
    one_hour = k3.usage_cost(input_tokens=1_000_000, output_tokens=0,
                             cache_write_tokens=1_000_000, cache_write_ttl=CACHE_TTL_1H)
    assert one_hour == Decimal("6.00")
    # kimi-k2.6 has no separate write charge: written tokens are miss input.
    k26 = PROFILES["kimi-k2.6"]
    assert k26.usage_cost(input_tokens=1_000_000, output_tokens=0, cached_input_tokens=500_000,
                          cache_write_tokens=500_000) == Decimal("0.08") + Decimal("0.475")
    # A malformed breakdown never produces a negative miss count.
    assert k3.usage_cost(input_tokens=10, output_tokens=0, cached_input_tokens=50) == \
        k3.usage_cost(input_tokens=10, output_tokens=0, cached_input_tokens=10)


def test_unrecognised_cache_ttl_is_priced_at_the_dearer_tier():
    parsed = read_usage(response("x", write=10, ttl="24h"))
    assert parsed.cache_write_ttl == CACHE_TTL_1H
    assert read_usage(response("x", write=10)).cache_write_ttl == "5m"


def _client(tracker, responses, calls):
    class Inner:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs)
                    return responses.pop(0)
    return build_guarded_client_factory(tracker, lambda *_: Inner())("k", "u")


def test_guarded_settlement_records_breakdown_and_emits_the_diagnostic_once_per_run():
    events, rows, calls = [], [], []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10), kill_switch=lambda: True,
                            event_emitter=lambda t, p: events.append((t, p)),
                            ledger_recorder=rows.append)
    client = _client(tracker, [response("{}", cached=200), response("{}"),
                               response("{}", reasoning=300)], calls)
    for _ in range(3):
        client.chat.completions.create(model="kimi-k3", messages=[{"content": "x"}],
                                       max_completion_tokens=1000)
    diagnostics = [p for t, p in events if t == "model_usage_diagnostic"]
    assert diagnostics == [{"message": diagnostics[0]["message"],
                            "payload": {"code": USAGE_REASONING_FIELD_ABSENT, "model": "kimi-k3"}}]
    ledger = tracker.ledger_snapshot()
    assert ledger["cached_input_tokens"] == 200
    assert ledger["reasoning_tokens"] == 300
    assert ledger["reasoning_estimated_calls"] == 2
    assert ledger["reasoning_tokens_estimated"] > 0
    settled = [r for r in rows if r["decision"] == "settled"]
    assert settled[0]["cached_input_tokens"] == 200 and settled[0]["reasoning_tokens"] is None
    assert settled[2]["reasoning_tokens"] == 300 and settled[2]["reasoning_estimated"] is False
    # Cost used the cache split: 200 hit + 800 miss, 500 output at K3 prices.
    first_cost = PROFILES["kimi-k3"].usage_cost(input_tokens=1000, output_tokens=500,
                                                cached_input_tokens=200)
    assert settled[0]["actual_cost"] == pytest.approx(float(first_cost))
    # Nothing durable carries reasoning text.
    assert "SECRET-CHAIN-OF-THOUGHT" not in repr(rows) + repr(events) + repr(ledger)


def test_the_diagnostic_is_not_repeated_after_a_resume():
    events, calls = [], []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10), kill_switch=lambda: True,
                            event_emitter=lambda t, p: events.append(t))
    tracker.restore_snapshot({"model_calls": 1, "reasoning_estimated_calls": 1})
    _client(tracker, [response("{}")], calls).chat.completions.create(
        model="kimi-k2.6", messages=[{"content": "x"}], max_tokens=100)
    assert "model_usage_diagnostic" not in events

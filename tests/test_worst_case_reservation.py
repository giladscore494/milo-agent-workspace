"""PR-R commit 5: worst-case cost reservation (spec 4.4 / 4.10 item 4)."""
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend.budget import (COST_RESERVATION_EXCEEDED, BudgetConfig, BudgetExceeded, BudgetTracker,
                            build_guarded_client_factory)
from backend.engines.swarm_v2.model_gateway import ModelGateway
from backend.model_profiles import PROFILES
from backend.provider_authority import conservative_input_tokens
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler

MESSAGES = [{"role": "system", "content": "plan"}, {"role": "user", "content": "x" * 400}]
K3 = PROFILES["kimi-k3"]


def worst_case(cap, messages=MESSAGES, response_format=None):
    # The input bound covers the messages AND the response_format material.
    fmt = response_format or {"type": "json_object"}
    fmt_bytes = len(json.dumps(fmt, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return float(K3.worst_case_cost(conservative_input_tokens(messages) + fmt_bytes, cap))


class Provider:
    """Counts HTTP-equivalent requests and observes the tracker mid-flight."""

    def __init__(self):
        self.requests = []
        self.in_flight_reserved_cost = []
        self.tracker = None

    def create(self, **kwargs):
        self.requests.append(kwargs)
        self.in_flight_reserved_cost.append(self.tracker.reserved_cost)
        message = SimpleNamespace(content='{"a": "b"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=200, completion_tokens=1000))


def stack(*, reservers=None, **budget):
    provider = Provider()
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=20, **budget),
                            kill_switch=lambda: True, **(reservers or {}))
    provider.tracker = tracker
    factory = build_guarded_client_factory(
        tracker, inner_factory=lambda *_: SimpleNamespace(chat=SimpleNamespace(completions=provider)))
    gateway = ModelGateway(
        guarded_client_factory=factory,
        scheduler=ProviderScheduler(ProviderLimitsConfig(
            max_concurrency=2, rpm_limit=None, max_rate_limit_retries=0,
            max_backpressure_wait_seconds=1, backoff_base_seconds=.001, backoff_max_seconds=.001)),
        api_key="offline", base_url="offline")
    return gateway, tracker, provider


def plan_call(gateway):
    return gateway.call(model="kimi-k3", agent="commander", phase="planning", messages=MESSAGES)


def test_the_worst_case_is_input_bound_at_the_reserve_rate_plus_the_whole_cap_at_output_rate():
    bound = conservative_input_tokens(MESSAGES)
    expected = (Decimal(bound) * Decimal("3.00") + Decimal(32_000) * Decimal("15.00")) / Decimal(1_000_000)
    assert K3.worst_case_cost(bound, 32_000) >= expected
    assert K3.worst_case_cost(bound, 32_000) - expected < Decimal("0.00000001")


def test_a_call_whose_reserve_exceeds_the_run_ceiling_is_never_sent():
    records = []
    gateway, tracker, provider = stack(max_cost_per_run=0.40)   # a 32k K3 plan is ~$0.48+
    tracker.ledger_recorder = records.append
    with pytest.raises(BudgetExceeded) as caught:
        plan_call(gateway)
    assert caught.value.code == COST_RESERVATION_EXCEEDED
    assert provider.requests == []                       # no HTTP request of any kind
    assert tracker.reserved_cost == 0 and tracker.actual_cost == 0
    assert records[-1] == {"decision": "rejected", "call_seq": 0,
                           "rejection_reason": COST_RESERVATION_EXCEEDED}


def test_in_flight_reserve_counts_against_the_ceiling_and_settle_releases_it():
    gateway, tracker, provider = stack(max_cost_per_run=3.00)
    plan_call(gateway)
    reserve = worst_case(32_000)
    assert provider.in_flight_reserved_cost == [pytest.approx(reserve)]
    # Settled: the reservation is gone and the ACTUAL cost replaces it.
    assert tracker.reserved_cost == 0
    assert tracker.actual_cost == pytest.approx(float(K3.usage_cost(input_tokens=200, output_tokens=1000)))
    # A double settlement of the same sequence hands nothing back.
    tracker.settle_call(0, 0, 0, 0, 0.0, call_seq=1)
    assert tracker.reserved_cost == 0


def test_actual_spend_plus_in_flight_reserves_are_what_the_next_call_is_checked_against():
    gateway, tracker, provider = stack(max_cost_per_run=1.00)
    tracker.actual_cost = 0.60                      # already spent this run
    with pytest.raises(BudgetExceeded) as caught:   # 0.60 + ~0.48 > 1.00
        plan_call(gateway)
    assert caught.value.code == COST_RESERVATION_EXCEEDED
    assert provider.requests == []


@pytest.mark.parametrize("dimension", ["user", "project"])
def test_the_remaining_daily_budget_is_checked_before_sending(dimension):
    gateway, tracker, provider = stack(
        **{f"daily_{dimension}_budget": 10.00},
        reservers={f"daily_{dimension}_cost_provider": lambda: 9.60})
    with pytest.raises(BudgetExceeded) as caught:
        plan_call(gateway)
    assert caught.value.code == COST_RESERVATION_EXCEEDED
    assert provider.requests == []


def test_the_atomic_daily_reservation_holds_the_worst_case_not_a_flat_rate():
    reserved = []
    settled = []

    def reserver(amount, call_seq):
        reserved.append((amount, call_seq))
        return {"id": f"r{call_seq}", "call_seq": call_seq, "estimated_cost": amount}

    gateway, tracker, provider = stack(
        daily_user_budget=10.00, estimated_cost_per_call=0.02,
        reservers={"daily_user_reserver": reserver,
                   "daily_settler": lambda r, cost, status, reason: settled.append((r.id, cost, status))})
    plan_call(gateway)
    assert reserved == [(pytest.approx(worst_case(32_000)), 1)]
    assert reserved[0][0] > 0.02
    assert settled == [("r1", pytest.approx(tracker.actual_cost), "settled")]


def test_the_database_refusal_still_refuses_before_sending():
    def reserver(amount, call_seq):
        return {"status": "rejected", "rejection_reason": "DAILY_USER_BUDGET_REACHED"}

    gateway, tracker, provider = stack(daily_user_budget=10.00,
                                       reservers={"daily_user_reserver": reserver})
    with pytest.raises(BudgetExceeded) as caught:
        plan_call(gateway)
    assert caught.value.code == "DAILY_USER_BUDGET_REACHED"
    assert provider.requests == [] and tracker.reserved_cost == 0


def test_a_provider_failure_releases_the_worst_case_reserve():
    gateway, tracker, provider = stack(max_cost_per_run=3.00)

    def boom(**kwargs):
        raise RuntimeError("connection reset")
    provider.create = boom
    with pytest.raises(Exception):
        plan_call(gateway)
    assert tracker.reserved_cost == 0


def test_v1_calls_keep_the_flat_reservation():
    reserved = []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=5, daily_user_budget=10.0,
                                         estimated_cost_per_call=0.02),
                            kill_switch=lambda: True,
                            daily_user_reserver=lambda amount, seq: reserved.append(amount) or f"r{seq}")
    inner = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1)))))
    build_guarded_client_factory(tracker, lambda *_: inner)("k", "u").chat.completions.create(
        model="kimi-k2.6", messages=[{"content": "x"}], max_tokens=10_000)
    assert reserved == [0.02] and tracker.reserved_cost == 0

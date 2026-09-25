"""PR-R commit 6: truncation is named and repaired once, in every role.

Includes the MANDATORY regression replay of run 4761a8ce (spec 4.10 item 2):
a Commander whose 4,000-token cap was consumed entirely by reasoning must end
in MODEL_REASONING_EXHAUSTED_OUTPUT with exactly ONE repair -- never in
COMMANDER_COMPLETION_SHAPE_INVALID, which is not repairable and killed that run.
"""
import json

import pytest

from backend.budget import (COST_RESERVATION_EXCEEDED, BudgetConfig, BudgetExceeded, BudgetTracker,
                            build_guarded_client_factory)
from backend.engines.swarm_v2 import (Commander, CommanderModelResolver, CommanderPlanFailure,
                                      PlanLimits, PlanValidator, Verifier, VerifierContractError)
from backend.engines.swarm_v2 import model_gateway
from backend.engines.swarm_v2.completion import (MODEL_EMPTY_COMPLETION, MODEL_OUTPUT_TRUNCATED,
                                                 MODEL_REASONING_EXHAUSTED_OUTPUT, CallShape,
                                                 escalate)
from backend.engines.swarm_v2.contracts import DynamicTask
from backend.engines.swarm_v2.model_gateway import ModelGateway
from backend.engines.swarm_v2.request_builder import RolePolicy
from backend.engines.swarm_v2.worker import GenericWorker
from backend.model_profiles import PROFILES
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler
from backend.tools import ToolContext, ToolRegistry
from tests.fakes.reasoning_provider import REASONING_MARKER, ReasoningProvider, reasoning_client
from tests.test_swarm_v2_smoke_offline import minimal_plan

PLAN_JSON = json.dumps(minimal_plan(num_tasks=1))


def stack(provider, *, events=None, **budget):
    rows = []
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=50, **budget),
                            kill_switch=lambda: True, ledger_recorder=rows.append,
                            event_emitter=(lambda t, p: events.append((t, p))) if events is not None
                            else None)
    gateway = ModelGateway(
        guarded_client_factory=build_guarded_client_factory(
            tracker, inner_factory=lambda *_: reasoning_client(provider)),
        scheduler=ProviderScheduler(ProviderLimitsConfig(
            max_concurrency=1, rpm_limit=None, max_rate_limit_retries=0,
            max_backpressure_wait_seconds=1, backoff_base_seconds=.001, backoff_max_seconds=.001)),
        api_key="offline", base_url="offline")
    return gateway, tracker, rows


def commander(gateway, model, retries):
    return Commander(client=gateway, resolver=CommanderModelResolver((model,), {model}),
                     validator=PlanValidator(allowed_tools=set(), limits=PlanLimits(max_tasks=64)),
                     retry_callback=lambda *args: retries.append(args))


def system_of(request):
    return " ".join(m["content"] for m in request["messages"] if m["role"] == "system")


# =============================================================================
# the 4761a8ce replay
# =============================================================================

def test_replay_4761a8ce_commander_reasoning_exhausts_the_cap_and_is_repaired_once(monkeypatch):
    """The run as it happened: kimi-k2.6, planning cap 4,000, the model thinking
    through the whole cap. 6,578 input + 4,000 output = 10,578 tokens."""
    monkeypatch.setitem(model_gateway.ROLE_POLICIES, ("commander", "planning"),
                        RolePolicy(effort="high", max_output=4_000, min_answer_reserve=1_000))
    provider = ReasoningProvider(reasoning_by_effort={"enabled": 4_000, "none": 0},
                                 answer=PLAN_JSON, prompt_tokens=6_578)
    events = []
    gateway, tracker, rows = stack(provider, events=events)
    retries = []

    plan = commander(gateway, "kimi-k2.6", retries).plan(
        requested_model="kimi-k2.6", objective="replay", context={})

    assert plan.graph.tasks[0].task_id == "t0"
    first, repair = provider.calls
    # 1. The failing call: thinking stated EXPLICITLY (4761a8ce sent nothing),
    #    the whole 4,000 cap spent reasoning, an empty answer, finish=length.
    assert first["extra_body"] == {"thinking": {"type": "enabled"}}
    assert first["max_tokens"] == 4_000
    # 2. Classified as MODEL_REASONING_EXHAUSTED_OUTPUT -- repairable -- and
    #    repaired EXACTLY once: one semantic retry, two provider calls.
    assert retries == [("commander", "planning", MODEL_REASONING_EXHAUSTED_OUTPUT)]
    assert MODEL_REASONING_EXHAUSTED_OUTPUT in system_of(repair)
    assert "COMMANDER_COMPLETION_SHAPE_INVALID" not in system_of(repair)
    # 3. Escalated, not repeated: the cap is already the role maximum, so the
    #    effort drops a notch -- for kimi-k2.6 that is thinking disabled.
    assert repair["extra_body"] == {"thinking": {"type": "disabled"}}
    assert repair["max_tokens"] == 4_000
    # 4. The truncated call was still paid for, at the CORRECTED k2.6 price:
    #    6,578 x 0.95 + 4,000 x 4.00 per 1M = $0.0222491 (the old table said
    #    $0.013947), and it is visible as reasoning, not as a mystery.
    settled = [row for row in rows if row["decision"] == "settled"]
    assert settled[0]["actual_input_tokens"] + settled[0]["actual_output_tokens"] == 10_578
    assert settled[0]["actual_cost"] == pytest.approx(0.0222491)
    assert settled[0]["reasoning_tokens"] == 4_000 and settled[0]["answer_tokens"] == 0
    assert tracker.model_calls == 2
    # 5. Reasoning text never reaches anything durable or any later prompt.
    durable = json.dumps([rows, events, tracker.ledger_snapshot()], default=str)
    assert REASONING_MARKER not in durable
    assert REASONING_MARKER not in json.dumps(repair["messages"])


def test_replay_4761a8ce_ends_in_the_named_code_when_the_repair_also_exhausts(monkeypatch):
    monkeypatch.setitem(model_gateway.ROLE_POLICIES, ("commander", "planning"),
                        RolePolicy(effort="high", max_output=4_000, min_answer_reserve=1_000))
    provider = ReasoningProvider(reasoning_by_effort={"enabled": 4_000, "none": 4_000},
                                 answer=PLAN_JSON)
    gateway, _tracker, _rows = stack(provider)
    retries = []
    with pytest.raises(CommanderPlanFailure) as caught:
        commander(gateway, "kimi-k2.6", retries).plan(
            requested_model="kimi-k2.6", objective="replay", context={})
    assert caught.value.code == MODEL_REASONING_EXHAUSTED_OUTPUT
    assert len(provider.calls) == 2 and len(retries) == 1


# =============================================================================
# the new K3 contract
# =============================================================================

def test_k3_commander_has_room_to_think_at_the_new_policy():
    provider = ReasoningProvider(reasoning_by_effort={"high": 20_000}, answer=PLAN_JSON)
    gateway, _tracker, _rows = stack(provider)
    retries = []
    commander(gateway, "kimi-k3", retries).plan(requested_model="kimi-k3", objective="o", context={})
    (call,) = provider.calls
    assert call["max_completion_tokens"] == 32_000 and call["reasoning_effort"] == "high"
    assert retries == []


def test_k3_exhaustion_at_the_role_maximum_lowers_the_effort_once():
    provider = ReasoningProvider(reasoning_by_effort={"high": 40_000, "low": 5_000},
                                 answer=PLAN_JSON)
    gateway, _tracker, _rows = stack(provider)
    retries = []
    commander(gateway, "kimi-k3", retries).plan(requested_model="kimi-k3", objective="o", context={})
    assert [c["reasoning_effort"] for c in provider.calls] == ["high", "low"]
    assert [c["max_completion_tokens"] for c in provider.calls] == [32_000, 32_000]


def test_nothing_to_escalate_means_no_repair_call():
    shape = CallShape(output_cap=12_000, effort="low")
    assert escalate("kimi-k3", "worker:t", "execute", shape) is None       # K3 cannot go below low
    assert escalate("kimi-k3", "worker:t", "execute", CallShape(6_000, "low")) == CallShape(12_000, "low")
    assert escalate("kimi-k2.6", "worker:t", "execute", CallShape(12_000, "none")) is None


def test_a_repair_whose_worst_case_does_not_fit_is_never_sent():
    """"Only if the reserve passes": the repair is an ordinary guarded call."""
    provider = ReasoningProvider(reasoning_by_effort={"high": 40_000, "low": 5_000},
                                 answer=PLAN_JSON)
    # The first 32k call's worst case (~$0.54) fits under $0.90; after paying
    # ~$0.48 for its truncated completion, the repair's worst case does not.
    gateway, tracker, _rows = stack(provider, max_cost_per_run=0.90)
    with pytest.raises(BudgetExceeded) as caught:
        commander(gateway, "kimi-k3", []).plan(requested_model="kimi-k3", objective="o", context={})
    assert caught.value.code == COST_RESERVATION_EXCEEDED
    assert len(provider.calls) == 1
    assert tracker.actual_cost <= 0.90 and tracker.reserved_cost == 0


def test_replanning_is_classified_and_never_repaired():
    provider = ReasoningProvider(reasoning_by_effort={"high": 20_000},
                                 answer='{"decision": "FINISH", "plan": null, "reason": "done"}')
    gateway, _tracker, _rows = stack(provider)
    with pytest.raises(CommanderPlanFailure) as caught:
        gateway.create_replan(model="kimi-k3", objective="o", summary={})
    assert caught.value.code == MODEL_REASONING_EXHAUSTED_OUTPUT
    assert len(provider.calls) == 1


# =============================================================================
# worker and verifier
# =============================================================================

def task():
    return DynamicTask.model_validate(minimal_plan(num_tasks=1)["graph"]["tasks"][0])


def worker(gateway, model, retries, events):
    return GenericWorker(gateway=gateway, tools=ToolRegistry(), model=model,
                         tool_context=ToolContext(),
                         retry_callback=lambda *args: retries.append(args),
                         event_sink=lambda t, p: events.append((t, p)))


def test_worker_reasoning_exhaustion_is_repaired_once_with_thinking_disabled():
    provider = ReasoningProvider(reasoning_by_effort={"enabled": 12_000, "none": 0})
    gateway, _tracker, _rows = stack(provider)
    retries, events = [], []
    result = worker(gateway, "kimi-k2.6", retries, events).execute(task(), {})
    assert result.status == "completed" and result.output == {"answer": "42"}
    assert [c["extra_body"]["thinking"]["type"] for c in provider.calls] == ["enabled", "disabled"]
    assert retries == [("worker:t0", "execute", MODEL_REASONING_EXHAUSTED_OUTPUT)]
    assert ("worker_output_repair_started",
            {"task_id": "t0", "reason_code": MODEL_REASONING_EXHAUSTED_OUTPUT,
             "attempt_number": 2}) in events


def test_worker_partial_answer_is_named_truncated_and_fails_with_its_code_after_one_repair():
    long_answer = json.dumps({"answer": "x " * 20_000})
    provider = ReasoningProvider(reasoning_by_effort={"low": 1_000}, answer=long_answer)
    gateway, _tracker, _rows = stack(provider)
    retries = []
    result = worker(gateway, "kimi-k3", retries, []).execute(task(), {})
    assert result.status == "failed"
    assert result.error["code"] == MODEL_OUTPUT_TRUNCATED
    # K3 worker at its 12k maximum and lowest effort: nothing to escalate, so
    # no repair call is bought.
    assert len(provider.calls) == 1 and retries == []


def test_worker_empty_answer_is_not_repaired():
    provider = ReasoningProvider(reasoning_by_effort={"enabled": 10}, answer="")
    gateway, _tracker, _rows = stack(provider)
    result = worker(gateway, "kimi-k2.6", [], []).execute(task(), {})
    assert result.error["code"] == MODEL_EMPTY_COMPLETION
    assert len(provider.calls) == 1


def _verifier_answer(request):
    document = json.loads(request["messages"][1]["content"])
    hashes = {source["source_id"]: [f["content_hash"] for f in source["fragments"]]
              for source in document["sources"]}
    return json.dumps({"verdicts": [{"claim_id": claim["claim_id"], "verdict": "verified",
                                     "supporting_fragment_hashes": hashes[claim["source_id"]][:1]}
                                    for claim in document["claims"]]})


def test_verifier_truncated_batch_is_repaired_once_by_escalation():
    from test_swarm_v2_verifier_batching import StubResolver, refs

    provider = ReasoningProvider(reasoning_by_effort={"high": 30_000, "low": 2_000},
                                 answer=_verifier_answer)
    gateway, _tracker, _rows = stack(provider)
    retries = []
    verdicts = Verifier(gateway=gateway, model="kimi-k3", resolver=StubResolver(),
                        retry_callback=lambda *args: retries.append(args)).verify(refs(2))
    assert [v.verdict for v in verdicts] == ["verified", "verified"]
    assert [c["reasoning_effort"] for c in provider.calls] == ["high", "low"]
    assert retries == [("verifier", "verification", MODEL_REASONING_EXHAUSTED_OUTPUT)]
    assert provider.calls[0]["response_format"]["json_schema"]["name"] == "verifier_batch"


def test_verifier_failure_after_the_repair_carries_the_static_code():
    from test_swarm_v2_verifier_batching import StubResolver, refs

    provider = ReasoningProvider(reasoning_by_effort={"high": 30_000, "low": 30_000},
                                 answer=_verifier_answer)
    gateway, _tracker, _rows = stack(provider)
    with pytest.raises(VerifierContractError) as caught:
        Verifier(gateway=gateway, model="kimi-k3", resolver=StubResolver()).verify(refs(1))
    assert caught.value.reason_code == MODEL_REASONING_EXHAUSTED_OUTPUT
    assert len(provider.calls) == 2


def test_absent_reasoning_field_is_estimated_and_diagnosed_once():
    provider = ReasoningProvider(reasoning_by_effort={"high": 3_000}, answer=PLAN_JSON,
                                 report_reasoning=False)
    events = []
    gateway, tracker, rows = stack(provider, events=events)
    commander(gateway, "kimi-k3", []).plan(requested_model="kimi-k3", objective="o", context={})
    settled = [row for row in rows if row["decision"] == "settled"]
    assert settled[0]["reasoning_tokens"] is None and settled[0]["reasoning_estimated"] is True
    assert settled[0]["reasoning_tokens_estimated"] == 3_000
    assert [p["payload"]["code"] for t, p in events if t == "model_usage_diagnostic"] == [
        "USAGE_REASONING_FIELD_ABSENT"]


def test_profiles_price_reasoning_as_output():
    k3 = PROFILES["kimi-k3"]
    # 30k reasoning + 2k answer = 32k output tokens, all at $15/1M.
    assert float(k3.usage_cost(input_tokens=0, output_tokens=32_000)) == pytest.approx(0.48)

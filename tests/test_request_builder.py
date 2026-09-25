"""PR-R commit 3: the profile-driven request builder (spec 4.3 / 4.10 item 5)."""
import json
from types import SimpleNamespace

import pytest

from backend.budget import BudgetConfig, BudgetTracker, apply_output_cap, build_guarded_client_factory
from backend.engines.swarm_v2.model_gateway import ModelGateway
from backend.engines.swarm_v2.request_builder import (MODEL_EFFORT_UNSUPPORTED,
                                                      MODEL_MESSAGE_INVALID,
                                                      MODEL_PARAM_FORBIDDEN,
                                                      ModelRequestRefused, RolePolicy,
                                                      build_provider_request, lower_effort)
from backend.model_profiles import PROFILES
from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler

K3, K26 = PROFILES["kimi-k3"], PROFILES["kimi-k2.6"]
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
          "additionalProperties": False}
HIGH = RolePolicy(effort="high", max_output=32_000, min_answer_reserve=6_000)
LOW = RolePolicy(effort="low", max_output=12_000, min_answer_reserve=3_000)


def test_k3_gets_max_completion_tokens_and_reasoning_effort_and_no_fixed_params():
    request = build_provider_request(K3, HIGH, MESSAGES, SCHEMA, schema_name="plan")
    assert request["max_completion_tokens"] == 32_000
    assert "max_tokens" not in request
    assert request["reasoning_effort"] == "high"
    for fixed in ("temperature", "top_p", "n", "presence_penalty", "frequency_penalty"):
        assert fixed not in request
    assert "extra_body" not in request and "thinking" not in json.dumps(request)
    assert request["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "plan", "strict": True, "schema": SCHEMA}}


def test_k26_always_gets_an_explicit_thinking_setting_and_never_reasoning_effort():
    enabled = build_provider_request(K26, LOW, MESSAGES, SCHEMA)
    assert enabled["extra_body"] == {"thinking": {"type": "enabled"}}
    assert enabled["max_tokens"] == 12_000 and "max_completion_tokens" not in enabled
    assert "reasoning_effort" not in enabled
    # k2.6 has no json_schema support in its profile: json_object.
    assert enabled["response_format"] == {"type": "json_object"}
    disabled = build_provider_request(K26, LOW, MESSAGES, effort="none")
    assert disabled["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("profile,policy", [(K3, HIGH), (K3, LOW), (K26, HIGH), (K26, LOW)])
def test_the_two_reasoning_controls_never_share_a_request(profile, policy):
    request = build_provider_request(profile, policy, MESSAGES, SCHEMA)
    has_effort = "reasoning_effort" in request
    has_thinking = "thinking" in request.get("extra_body", {})
    assert has_effort != has_thinking


def test_forbidden_and_unknown_parameters_are_refused_not_dropped():
    for extra in ({"temperature": 0.2}, {"top_p": 1}, {"thinking": {"type": "disabled"}},
                  {"prompt_cache_key": "x"}):
        with pytest.raises(ModelRequestRefused) as caught:
            build_provider_request(K3, HIGH, MESSAGES, extra=extra)
        assert caught.value.code == MODEL_PARAM_FORBIDDEN


def test_k3_cannot_be_asked_not_to_reason_and_reasoning_content_is_never_sent():
    with pytest.raises(ModelRequestRefused) as caught:
        build_provider_request(K3, HIGH, MESSAGES, effort="none")
    assert caught.value.code == MODEL_EFFORT_UNSUPPORTED
    replay = [*MESSAGES, {"role": "assistant", "content": "x", "reasoning_content": "thought"}]
    with pytest.raises(ModelRequestRefused) as caught:
        build_provider_request(K3, HIGH, replay)
    assert caught.value.code == MODEL_MESSAGE_INVALID


def test_effort_ladder():
    assert [lower_effort(K3, e) for e in ("max", "high", "low")] == ["high", "low", None]
    assert lower_effort(K26, "low") == "none" and lower_effort(K26, "none") is None


def test_apply_output_cap_writes_exactly_one_field_under_the_profile_spelling():
    request = {"max_tokens": 5, "max_completion_tokens": 7}
    assert apply_output_cap(request, 9, "max_completion_tokens") == {"max_completion_tokens": 9}
    assert apply_output_cap({"max_completion_tokens": 7}, 9) == {"max_tokens": 9}
    with pytest.raises(ValueError):
        apply_output_cap({}, 9, "max_output_tokens")


def _gateway(calls):
    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            message = SimpleNamespace(content='{"a": "b"}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                                   usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    tracker = BudgetTracker(BudgetConfig(max_model_calls_per_run=10), kill_switch=lambda: True)
    factory = build_guarded_client_factory(
        tracker, inner_factory=lambda *_: SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    return ModelGateway(
        guarded_client_factory=factory,
        scheduler=ProviderScheduler(ProviderLimitsConfig(
            max_concurrency=1, rpm_limit=None, max_rate_limit_retries=0,
            max_backpressure_wait_seconds=1, backoff_base_seconds=.001, backoff_max_seconds=.001)),
        api_key="offline", base_url="offline")


def test_the_wire_request_through_the_real_guarded_client_keeps_the_k3_contract():
    calls = []
    _gateway(calls).call(model="kimi-k3", agent="worker:t1", phase="execute",
                         messages=MESSAGES, schema=SCHEMA, schema_name="worker_output")
    (sent,) = calls
    assert "max_completion_tokens" in sent and "max_tokens" not in sent
    assert sent["reasoning_effort"] == "low"
    assert sent["response_format"]["type"] == "json_schema"
    assert "temperature" not in sent


def test_the_gateway_refuses_a_forbidden_parameter_before_any_provider_request():
    calls = []
    gateway = _gateway(calls)
    with pytest.raises(ModelRequestRefused) as caught:
        gateway.call(model="kimi-k3", agent="verifier", phase="verification",
                     messages=MESSAGES, temperature=0.1)
    assert caught.value.code == MODEL_PARAM_FORBIDDEN
    with pytest.raises(ModelRequestRefused) as caught:
        gateway.call(model="kimi-k9", agent="verifier", phase="verification", messages=MESSAGES)
    assert caught.value.code == "MODEL_PROFILE_UNKNOWN"
    assert calls == []

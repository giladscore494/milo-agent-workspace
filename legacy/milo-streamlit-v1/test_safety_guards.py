import json
from types import SimpleNamespace
from unittest.mock import patch

from app import (
    MAX_DISCOVERY_FALLBACK_TOKENS,
    MAX_DISCOVERY_TOKENS,
    RAW_DEBUG_PREVIEW_CHARS,
    AgentConfig,
    detect_planning_or_repetition_loop,
    discovery_prompt,
    format_debug_json,
    merge_discovery_candidates,
    moonshot_chat,
    run_discovery_agent,
    validate_discovery_schema,
    validate_model_response,
)


def result(content, finish_reason="stop"):
    return {"content": content, "finish_reason": finish_reason, "input_tokens": 11, "output_tokens": 22, "parsed": None}


def valid_discovery_text():
    return json.dumps({
        "manufacturer": "Hyundai",
        "market": "Israel",
        "period": "2010 to June 2026",
        "models": [{
            "model_name_en": "i10",
            "source_url": "https://example.com",
        }],
    })


def test_repeated_i_need_to_search_rejected():
    looped, reason = detect_planning_or_repetition_loop("I need to search. " * 4)
    assert looped
    assert reason in {"MODEL_PLANNING_LOOP", "MODEL_REPETITION_LOOP"}


def test_repeated_let_me_search_again_rejected():
    checked = validate_model_response(result("Let me search again. " * 3), require_json=True)
    assert checked["_error"] in {"MODEL_PLANNING_LOOP", "MODEL_REPETITION_LOOP"}


def test_finish_reason_length_rejected():
    checked = validate_model_response(result('{"ok": true}', finish_reason="length"), require_json=True)
    assert checked["_error"] == "MODEL_OUTPUT_TRUNCATED"
    assert checked["finish_reason"] == "length"


def test_finish_reason_length_partial_json_gets_clear_error():
    checked = validate_model_response(result('{"models":[{"model_name_en":"Santa Fe"', finish_reason="length"), require_json=True)
    assert checked["_error"] == "MODEL_JSON_TRUNCATED"
    assert "valid-looking JSON" in checked["message"]


def test_invalid_json_rejected():
    checked = validate_model_response(result("not json"), require_json=True)
    assert checked["_error"] == "INVALID_JSON"


def test_valid_discovery_json_object_passes():
    checked = validate_model_response(result(valid_discovery_text()), require_json=True, validator=validate_discovery_schema)
    assert "_error" not in checked
    assert checked["parsed"]["models"][0]["model_name_en"] == "i10"


def test_discovery_validator_strips_extra_fields():
    parsed = {"agent": "a", "models": [{"model_name_en": "i10", "body_type": "hatch", "confidence": "high", "sources": ["https://example.com"]}]}
    assert validate_discovery_schema(parsed) is None
    assert parsed["models"] == [{"model_name_en": "i10", "source_url": "https://example.com"}]


def test_failed_raw_preview_is_capped_to_2000_chars():
    checked = validate_model_response(result("x" * 5000, finish_reason="length"), require_json=True)
    assert len(checked["raw_preview"]) == RAW_DEBUG_PREVIEW_CHARS == 2000


class FakeMessage:
    content = '{"ok": true}'
    tool_calls = None

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class FakeResponse:
    def __init__(self):
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2)
        self.choices = [SimpleNamespace(finish_reason="stop", message=FakeMessage())]


class FakeCompletions:
    def __init__(self):
        self.kwargs = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        return FakeResponse()


class FakeClient:
    completions = FakeCompletions()

    def __init__(self, *args, **kwargs):
        self.chat = SimpleNamespace(completions=self.completions)


def test_moonshot_chat_passes_max_tokens_normal_and_retry_paths():
    FakeClient.completions = FakeCompletions()
    with patch("app.OpenAI", FakeClient):
        moonshot_chat("key", [{"role": "user", "content": "hi"}], temperature=0.6, use_web_search=False, max_tokens=123)
        assert FakeClient.completions.kwargs[-1]["max_tokens"] == 123

    FakeClient.completions = FakeCompletions()
    with patch("app.OpenAI", FakeClient):
        moonshot_chat("key", [{"role": "user", "content": "hi"}], temperature=0.6, use_web_search=True, max_tokens=456)
        assert len(FakeClient.completions.kwargs) == 2
        assert all(call["max_tokens"] == 456 for call in FakeClient.completions.kwargs)


def test_python_merge_deduplicates_model_names():
    merged = merge_discovery_candidates([
        {"status": "success", "agent": "a1", "parsed": {"manufacturer": "Hyundai", "market": "Israel", "period": "2010-2026", "models": [
            {"model_name_en": "Hyundai Tucson", "source_url": "https://a"}
        ]}},
        {"status": "success", "agent": "a2", "parsed": {"manufacturer": "Hyundai", "market": "Israel", "period": "2010-2026", "models": [
            {"model_name_en": "Tucson", "source_url": "https://b"}
        ]}},
    ])
    assert len(merged["candidate_models"]) == 1
    assert set(merged["candidate_models"][0]["sources"]) == {"https://a", "https://b"}


def test_python_merge_rejects_obvious_trim_package_names():
    merged = merge_discovery_candidates([
        {"status": "success", "agent": "a1", "parsed": {"manufacturer": "Hyundai", "market": "Israel", "period": "2010-2026", "models": [
            {"model_name_en": "N Line", "source_url": "https://a"},
            {"model_name_en": "i20", "source_url": "https://b"},
        ]}},
    ])
    assert [x["canonical_model_name"] for x in merged["candidate_models"]] == ["i20"]
    assert merged["rejected_candidates"][0]["reason"] == "trim_or_package_not_model"


def test_discovery_fallback_attempted_at_most_once():
    agent = AgentConfig("current_official_lineup_agent", "Current", "Current", "Find current models")
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(kwargs["phase_name"])
        return {"content": "I need to search. " * 4, "finish_reason": "stop", "input_tokens": 1, "output_tokens": 2, "agent": kwargs["agent_name"], "phase": kwargs["phase_name"]}

    with patch("app.moonshot_chat", side_effect=fake_call):
        result = run_discovery_agent("key", agent, "Hyundai", "Israel", "2010 to June 2026")

    assert result["status"] == "failed"
    assert result["used_fallback"] is True
    assert calls == ["discovery", "discovery_fallback"]


def test_discovery_prompt_is_compact_candidate_schema_only():
    agent = AgentConfig("current_official_lineup_agent", "Current", "Current", "Find current models")
    prompt = "\n".join(m["content"] for m in discovery_prompt(agent, "Hyundai", "Israel", "2010 to June 2026"))
    assert "model_name_en" in prompt
    assert "source_url" in prompt
    assert "currently_sold" not in prompt
    assert "body_type" not in prompt
    assert "years_sold" not in prompt
    assert "generations" not in prompt
    assert "notes" not in prompt
    assert len(prompt) < 1400


def test_discovery_token_limits_are_moderately_increased():
    assert MAX_DISCOVERY_TOKENS == 1800
    assert MAX_DISCOVERY_FALLBACK_TOKENS == 1200


def test_debug_json_uses_json_dumps_formatting():
    rendered = format_debug_json({"_error": "MODEL_JSON_TRUNCATED", "finish_reason": "length"})
    assert '"_error": "MODEL_JSON_TRUNCATED",' in rendered
    assert '"finish_reason": "length"' in rendered

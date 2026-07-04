import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import app
from app import AgentConfig


AGENT_KEYS = [
    "current_official_lineup_agent",
    "historical_used_market_agent",
    "ev_hybrid_edge_cases_agent",
    "normalizer_deduper",
    "trims_years_agent",
    "engines_fuel_power_agent",
    "transmission_drivetrain_performance_agent",
    "dimensions_safety_equipment_agent",
    "source_verifier",
    "final_builder",
]


def ok_payload(agent="x"):
    return {"agent": agent, "items": [], "missing_data": [], "extra_candidate_models": []}


def fake_result(content, finish_reason="stop", agent="x", phase="p"):
    return {"content": content, "finish_reason": finish_reason, "input_tokens": 1, "output_tokens": 2, "agent": agent, "phase": phase}


def test_discovery_still_accepts_ultra_thin_schema():
    parsed = {"agent": "current_official_lineup_agent", "models": [{"model_name_en": "i10", "source_url": None}]}
    assert app.validate_discovery_schema(parsed) is None
    assert parsed["models"] == [{"model_name_en": "i10", "source_url": None}]


def test_normalizer_requires_canonical_models_list():
    assert app.validate_normalizer_schema({"agent": "normalizer_deduper", "canonical_models": {}, "rejected_items": [], "needs_review": []}) == "INVALID_NORMALIZER_SCHEMA"


def test_agents_3_to_8_use_shared_safe_wrapper():
    calls = []

    def fake_safe(*args, **kwargs):
        calls.append(kwargs["agent_name"])
        return app.phase_result(status="success", agent=kwargs["agent_name"], parsed={"ok": True})

    models = [{"canonical_model_name": "i10", "sources": []}]
    with patch("app.run_safe_agent", side_effect=fake_safe):
        for agent in app.TECHNICAL_AGENTS:
            app.run_technical_agent("key", agent, "Hyundai", "Israel", "2010-2026", models)
        app.run_verification_phase("key", {}, {})
        app.run_final_builder_phase("key", {}, {}, {}, [], "Hyundai", "Israel", "2010-2026")

    assert calls == [
        "trims_years_agent",
        "engines_fuel_power_agent",
        "transmission_drivetrain_performance_agent",
        "dimensions_safety_equipment_agent",
        "source_verifier",
    ]


def test_finish_reason_length_fails_for_every_agent():
    for agent in AGENT_KEYS:
        checked = app.validate_model_response(fake_result('{"agent":"x"}', finish_reason="length", agent=agent), require_json=True)
        assert checked["_error"] == "MODEL_OUTPUT_TRUNCATED"


def test_invalid_json_fails_for_every_agent():
    for agent in AGENT_KEYS:
        checked = app.validate_model_response(fake_result("not json", agent=agent), require_json=True)
        assert checked["_error"] == "INVALID_JSON"


def test_planning_loop_fails_for_every_agent():
    for agent in AGENT_KEYS:
        checked = app.validate_model_response(fake_result("I need to search. " * 4, agent=agent), require_json=True)
        assert checked["_error"] in {"MODEL_PLANNING_LOOP", "MODEL_REPETITION_LOOP"}


def test_429_concurrency_error_triggers_backoff_retry_and_classification(monkeypatch):
    calls = {"n": 0, "sleep": 0}

    def fake_chat(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("Error code: 429 request reached max organization concurrency: 3")

    monkeypatch.setattr(app, "moonshot_chat", fake_chat)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: calls.__setitem__("sleep", calls["sleep"] + 1))
    result = app.run_safe_agent(
        "key", agent_name="engines_fuel_power_agent", phase_name="technical", prompt=[{"role": "user", "content": "x"}],
        max_tokens=3000, required_top_keys=["agent"], use_web_search=True,
    )
    assert result["status"] == "failed"
    assert result["error"] == "API_CONCURRENCY_LIMIT"
    assert result["api_retry_count"] == 2
    assert calls == {"n": 3, "sleep": 2}


def test_429_does_not_use_fallback_prompt(monkeypatch):
    phases = []

    def fake_chat(*args, **kwargs):
        phases.append(kwargs["phase_name"])
        raise RuntimeError("rate_limit_reached_error HTTP 429 max organization concurrency")

    monkeypatch.setattr(app, "moonshot_chat", fake_chat)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)
    result = app.run_safe_agent(
        "key", agent_name="trims_years_agent", phase_name="technical", prompt=[{"role": "user", "content": "x"}],
        fallback_prompt=[{"role": "user", "content": "fallback"}], max_tokens=2500, required_top_keys=["agent"], use_web_search=True,
    )
    assert result["error"] == "API_CONCURRENCY_LIMIT"
    assert set(phases) == {"technical"}


def test_technical_phase_runs_sequentially_without_executor():
    order = []

    def fake_agent(api_key, agent, manufacturer, market, period, canonical_models):
        order.append((agent.key, [m["canonical_model_name"] for m in canonical_models]))
        return app.phase_result(status="success", agent=agent.key, parsed={})

    models = [{"canonical_model_name": f"m{i}"} for i in range(5)]
    with patch("app.run_technical_agent", side_effect=fake_agent):
        app.run_technical_enrichment_phase("key", "Hyundai", "Israel", "2010-2026", models)
    assert order == [
        item
        for a in app.TECHNICAL_AGENTS
        for item in ((a.key, ["m0", "m1", "m2", "m3"]), (a.key, ["m4"]))
    ]


def test_global_kimi_semaphore_limits_parallel_calls(monkeypatch):
    active = 0
    max_active = 0
    lock = threading.Lock()

    class FakeMessage:
        content = '{"ok": true}'
        tool_calls = None
        def model_dump(self, exclude_none=True):
            return {"role": "assistant", "content": self.content}

    class FakeResponse:
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        choices = [SimpleNamespace(finish_reason="stop", message=FakeMessage())]

    class FakeCompletions:
        def create(self, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return FakeResponse()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(app, "OpenAI", FakeClient)
    threads = [threading.Thread(target=app.moonshot_chat, args=("key", [{"role": "user", "content": "hi"}]), kwargs={"temperature": 0.6, "use_web_search": False, "max_tokens": 10}) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert max_active <= app.MAX_PARALLEL_KIMI_CALLS == 2


def test_raw_failed_output_is_capped_to_constant():
    checked = app.validate_model_response(fake_result("x" * 5000, finish_reason="length"), require_json=True)
    assert len(checked["raw_preview"]) == app.RAW_DEBUG_PREVIEW_CHARS


def test_final_builder_does_not_call_llm_or_preserve_raw_failed_text():
    with patch("app.run_safe_agent", side_effect=AssertionError("final builder must not call LLM")):
        result = app.run_final_builder_phase("key", {"canonical_models": []}, {}, {}, [{"agent": "a", "error": "INVALID_JSON", "raw_preview": "SECRET_RAW"}], "Hyundai", "Israel", "2010-2026")
    output_text = json.dumps(result, ensure_ascii=False)
    assert result["finish_reason"] == "python_merge_success"
    assert "SECRET_RAW" not in output_text
    assert "INVALID_JSON" in output_text


def test_global_only_sources_are_marked_needs_review_by_verifier_validator():
    parsed = {"agent": "source_verifier", "verified_models": [{"model": "Ioniq", "status": "verified", "confidence": "high", "issues": ["hyundainews.com"], "source_region": "global", "source_strength": "global_official"}], "rejected_data_points": [], "needs_review": []}
    assert app.validate_verifier_schema(parsed) is None
    assert parsed["verified_models"][0]["status"] == "needs_review"
    assert parsed["verified_models"][0]["confidence"] == "medium"


def test_if_enrichment_agent_fails_final_status_becomes_partial_success():
    result = app.run_final_builder_phase("key", {"canonical_models": [{"canonical_model_name": "i10"}]}, {}, {}, [{"agent": "engines", "error": "INVALID_JSON"}], "Hyundai", "Israel", "2010-2026")
    assert result["parsed"]["status"] == "partial_success"


def test_technical_json_missing_agent_is_repaired_from_agent_name():
    checked = app.validate_model_response(
        fake_result('{"items":[],"missing_data":[],"extra_candidate_models":[]}', agent="trims_years_agent", phase="technical"),
        require_json=True,
        required_keys=["agent", "items", "missing_data", "extra_candidate_models"],
        validator=app.validate_items_schema,
    )
    assert checked["parsed"]["agent"] == "trims_years_agent"
    assert "agent" in checked["repaired_fields"]


def test_technical_json_missing_items_still_fails():
    checked = app.validate_model_response(
        fake_result('{"missing_data":[],"extra_candidate_models":[]}', agent="trims_years_agent", phase="technical"),
        require_json=True,
        required_keys=["agent", "items", "missing_data", "extra_candidate_models"],
        validator=app.validate_items_schema,
    )
    assert checked["_error"] == "MISSING_REQUIRED_KEY:items"


def test_technical_json_missing_optional_lists_default_to_empty():
    checked = app.validate_model_response(
        fake_result('{"agent":"engines_fuel_power_agent","items":[]}', agent="engines_fuel_power_agent", phase="technical"),
        require_json=True,
        required_keys=["agent", "items", "missing_data", "extra_candidate_models"],
        validator=app.validate_items_schema,
    )
    assert checked["parsed"]["missing_data"] == []
    assert checked["parsed"]["extra_candidate_models"] == []


def test_required_keys_are_validated_against_parsed_not_wrapper(monkeypatch):
    def fake_chat(*args, **kwargs):
        return fake_result('{"items":[],"missing_data":[],"extra_candidate_models":[]}', agent="engines_fuel_power_agent", phase="technical")

    monkeypatch.setattr(app, "moonshot_chat", fake_chat)
    result = app.run_safe_agent(
        "key", agent_name="engines_fuel_power_agent", phase_name="technical", prompt=[{"role": "user", "content": "x"}],
        max_tokens=20, required_top_keys=["agent", "items", "missing_data", "extra_candidate_models"], validator=app.validate_items_schema,
    )
    assert result["status"] == "success"
    assert result["agent"] == "engines_fuel_power_agent"
    assert result["parsed"]["agent"] == "engines_fuel_power_agent"


def test_chunk_merge_includes_agent_at_wrapper_and_parsed_levels():
    merged = app.merge_chunk_results("engines_fuel_power_agent", [app.phase_result(status="success", agent="engines_fuel_power_agent", parsed={"items": [], "missing_data": [], "extra_candidate_models": []})])
    assert merged["agent"] == "engines_fuel_power_agent"
    assert merged["parsed"]["agent"] == "engines_fuel_power_agent"


def test_fallback_prompt_for_each_technical_agent_includes_agent():
    for agent in app.TECHNICAL_AGENTS:
        prompt_text = "\n".join(m["content"] for m in app.technical_fallback_prompt(agent, "Hyundai", "Israel", "2010-2026", [{"canonical_model_name": "i10", "sources": ["u"]}]))
        assert f'"agent": "{agent.key}"' in prompt_text


def test_technical_agents_are_called_with_chunks_of_max_four(monkeypatch):
    chunk_lengths = []

    def fake_agent(api_key, agent, manufacturer, market, period, canonical_models):
        chunk_lengths.append(len(canonical_models))
        return app.phase_result(status="success", agent=agent.key, parsed={"agent": agent.key, "items": [], "missing_data": [], "extra_candidate_models": []})

    monkeypatch.setattr(app, "run_technical_agent", fake_agent)
    models = [{"canonical_model_name": f"m{i}", "aliases": ["a", "b", "c", "d"]} for i in range(9)]
    app.run_technical_enrichment_phase("key", "Hyundai", "Israel", "2010-2026", models)
    assert app.TECHNICAL_MODEL_CHUNK_SIZE == 4
    assert chunk_lengths == [4, 4, 1] * len(app.TECHNICAL_AGENTS)


def test_fallback_prompt_preserves_same_compact_model_chunk_and_requires_one_item():
    agent = app.TECHNICAL_AGENTS[1]
    models = [
        {"canonical_model_name": "i10", "aliases": ["a1", "a2", "a3", "a4"], "sources": ["s1", "s2", "s3"]},
        {"canonical_model_name": "i20", "aliases": ["b1"], "sources": ["s4"]},
    ]
    prompt_text = "\n".join(m["content"] for m in app.technical_fallback_prompt(agent, "Hyundai", "Israel", "2010-2026", models))
    assert "i10" in prompt_text and "i20" in prompt_text
    assert "a4" not in prompt_text
    assert "s3" not in prompt_text
    assert "For each model, return at most one item" in prompt_text
    assert "One compact item per model" in prompt_text


def test_run_technical_agent_uses_reduced_fallback_token_limit(monkeypatch):
    captured = {}

    def fake_safe(*args, **kwargs):
        captured.update(kwargs)
        return app.phase_result(status="success", agent=kwargs["agent_name"], parsed={})

    monkeypatch.setattr(app, "run_safe_agent", fake_safe)
    app.run_technical_agent("key", app.TECHNICAL_AGENTS[1], "Hyundai", "Israel", "2010-2026", [{"canonical_model_name": "i10"}])
    assert captured["fallback_max_tokens"] == 1200


def test_verbose_technical_shapes_are_rejected_and_compact_fields_are_capped():
    verbose_keys = [
        ("trims_years_agent", {"model": "i10", "trims_by_year": []}),
        ("engines_fuel_power_agent", {"model": "i10", "engines_fuel_power": []}),
        ("transmission_drivetrain_performance_agent", {"model": "i10", "transmission_drivetrain_performance": {}}),
        ("dimensions_safety_equipment_agent", {"model": "i10", "dimensions": {}, "safety_equipment": {}}),
    ]
    for agent, item in verbose_keys:
        parsed = {"agent": agent, "items": [item], "missing_data": [], "extra_candidate_models": []}
        assert app.validate_items_schema(parsed) == "TECHNICAL_OUTPUT_TOO_VERBOSE"

    parsed = {
        "agent": "trims_years_agent",
        "items": [{
            "canonical_model_name": "i10",
            "sources": ["s1", "s2", "s3"],
            "notes": "x" * 200,
            "trims": ["1", "2", "3", "4", "5", "6", "7"],
            "extra": "drop",
        }],
        "missing_data": [],
        "extra_candidate_models": [],
    }
    assert app.validate_items_schema(parsed) is None
    item = parsed["items"][0]
    assert item["model"] == "i10"
    assert len(item["sources"]) == 2
    assert len(item["notes"]) == 160
    assert item["trims"] == ["1", "2", "3", "4", "5", "6"]
    assert "extra" not in item


def test_phase_3_truncation_message_is_not_discovery_specific():
    checked = app.validate_model_response(
        fake_result('{"agent":"trims_years_agent","items":[', finish_reason="length", agent="trims_years_agent", phase="technical"),
        require_json=True,
    )
    assert checked["_error"] == "MODEL_JSON_TRUNCATED"
    assert "Technical agent produced" in checked["message"]
    assert "Discovery" not in checked["message"]
    assert "MAX_DISCOVERY_TOKENS" not in checked["message"]


def test_technical_chunk_merge_is_partial_when_one_chunk_truncated():
    merged = app.merge_chunk_results("trims_years_agent", [
        app.phase_result(status="failed", agent="trims_years_agent", error="MODEL_JSON_TRUNCATED"),
        app.phase_result(status="success", agent="trims_years_agent", parsed={"agent": "trims_years_agent", "items": [{"model": "i10"}], "missing_data": [], "extra_candidate_models": []}),
    ])
    assert merged["status"] == "partial"
    assert merged["parsed"]["items"] == [{"model": "i10"}]


def test_technical_prompt_includes_required_chunk_context_and_schema():
    agent = app.TECHNICAL_AGENTS[1]
    models = [{"canonical_model_name": "i10", "sources": ["u"]}]
    prompt_text = "\n".join(m["content"] for m in app.technical_prompt(agent, "Hyundai", "Israel", "2010-2026", models))
    assert "Manufacturer: Hyundai" in prompt_text
    assert "Market: Israel" in prompt_text
    assert "Period: 2010-2026" in prompt_text
    assert "Agent name: engines_fuel_power_agent" in prompt_text
    assert "Concrete canonical model chunk" in prompt_text
    assert "canonical_model_name" in prompt_text
    assert "Return exactly this JSON schema shape" in prompt_text


def test_technical_fallback_prompt_includes_required_chunk_context_and_schema():
    agent = app.TECHNICAL_AGENTS[0]
    models = [{"canonical_model_name": "i10", "sources": ["u"]}]
    prompt_text = "\n".join(m["content"] for m in app.technical_fallback_prompt(agent, "Hyundai", "Israel", "2010-2026", models))
    assert "Manufacturer: Hyundai" in prompt_text
    assert "Market: Israel" in prompt_text
    assert "Period: 2010-2026" in prompt_text
    assert "Agent name: trims_years_agent" in prompt_text
    assert "Concrete canonical model chunk" in prompt_text
    assert "canonical_model_name" in prompt_text
    assert "Required exact JSON schema" in prompt_text


def test_generic_automotive_top_level_keys_are_rejected():
    checked = app.validate_model_response(
        fake_result('{"engine_types":["gasoline"],"agent":"engines_fuel_power_agent","items":[],"missing_data":[],"extra_candidate_models":[]}', agent="engines_fuel_power_agent", phase="technical"),
        require_json=True,
        required_keys=["agent", "items", "missing_data", "extra_candidate_models"],
        validator=app.validate_items_schema,
    )
    assert checked["_error"] == "GENERIC_AUTOMOTIVE_OUTPUT"


def test_missing_make_model_message_is_not_repaired_as_missing_agent():
    checked = app.validate_model_response(
        fake_result('{"message":"Please provide the make and model."}', agent="trims_years_agent", phase="technical"),
        require_json=True,
        required_keys=["agent", "items", "missing_data", "extra_candidate_models"],
        validator=app.validate_items_schema,
    )
    assert checked["_error"] == "AGENT_DID_NOT_RECEIVE_MODEL_CHUNK"

def test_verifier_input_excludes_raw_preview_and_raw_failed_text():
    compact = app.compact_verifier_input(
        {"canonical_models": [{"canonical_model_name": "i10", "sources": ["u"]}]},
        {"a": {"agent": "a", "items": [{"model": "i10", "sources": ["u"], "notes": "raw notes"}], "missing_data": [], "extra_candidate_models": []}},
        [{"agent": "a", "error": "INVALID_JSON", "raw_preview": "SECRET_RAW", "message": "safe"}],
    )
    text = json.dumps(compact, ensure_ascii=False)
    assert "raw_preview" not in text
    assert "SECRET_RAW" not in text
    assert "INVALID_JSON" in text


def test_verifier_chunks_large_model_lists(monkeypatch):
    calls = []

    def fake_safe(*args, **kwargs):
        calls.append(kwargs["prompt"])
        return app.phase_result(status="success", agent="source_verifier", parsed={"agent": "source_verifier", "verified_models": [], "rejected_data_points": [], "needs_review": []})

    monkeypatch.setattr(app, "run_safe_agent", fake_safe)
    normalized = {"canonical_models": [{"canonical_model_name": f"m{i}", "sources": []} for i in range(app.VERIFIER_MODEL_CHUNK_SIZE + 1)]}
    result = app.run_verification_phase("key", normalized, {})
    assert result["status"] == "success"
    assert len(calls) == 2


def test_verifier_truncated_one_chunk_produces_partial_if_other_chunks_succeed(monkeypatch):
    responses = [
        app.phase_result(status="failed", agent="source_verifier", error="MODEL_JSON_TRUNCATED"),
        app.phase_result(status="success", agent="source_verifier", parsed={"agent": "source_verifier", "verified_models": [{"model": "m"}], "rejected_data_points": [], "needs_review": []}),
    ]

    def fake_safe(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(app, "run_safe_agent", fake_safe)
    normalized = {"canonical_models": [{"canonical_model_name": f"m{i}", "sources": []} for i in range(app.VERIFIER_MODEL_CHUNK_SIZE + 1)]}
    result = app.run_verification_phase("key", normalized, {})
    assert result["status"] == "partial"
    assert result["parsed"]["agent"] == "source_verifier"
    assert result["parsed"]["verified_models"] == [{"model": "m"}]


def test_if_all_technical_agents_fail_verifier_and_final_builder_do_not_run(monkeypatch):
    events = []

    class FakeSessionState(dict):
        def __getattr__(self, name):
            return self[name]
        def __setattr__(self, name, value):
            self[name] = value

    class FakeSt:
        def __init__(self):
            self.session_state = FakeSessionState()
        def subheader(self, text): events.append(("subheader", text))
        def write(self, text): pass
        def code(self, *args, **kwargs): pass
        def success(self, text): pass
        def warning(self, text): events.append(("warning", text))
        def error(self, text): events.append(("error", text))

    fake_st = FakeSt()
    fake_st.session_state.results = {}
    fake_st.session_state.input_tokens = 0
    fake_st.session_state.output_tokens = 0
    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "run_discovery_phase", lambda *args: [app.phase_result(status="success", agent="d", parsed={"agent":"d","manufacturer":"Hyundai","market":"Israel","period":"p","models":[{"model_name_en":"i10","source_url":"u"}]})])
    monkeypatch.setattr(app, "run_normalizer_phase", lambda *args: app.phase_result(status="success", agent="normalizer_deduper", parsed={"agent":"normalizer_deduper","canonical_models":[{"canonical_model_name":"i10","sources":["u"]}],"rejected_items":[],"needs_review":[]}))
    monkeypatch.setattr(app, "run_technical_enrichment_phase", lambda *args: [app.phase_result(status="failed", agent=a.key, error="INVALID_JSON") for a in app.TECHNICAL_AGENTS])
    monkeypatch.setattr(app, "run_verification_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("verifier should not run")))
    monkeypatch.setattr(app, "run_final_builder_phase", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("final builder should not run")))

    app.run_pipeline("key", "Hyundai", "Israel", "p")
    assert ("error", "Pipeline failed: TECHNICAL_ENRICHMENT_FAILED\nReason: all technical enrichment agents failed.") in events


def test_python_final_builder_merges_canonical_models_with_technical_items():
    normalized = {"canonical_models": [{"canonical_model_name": "Ioniq 5", "aliases": ["IONIQ5"], "sources": ["official"], "confidence": "medium"}]}
    technical = {
        "trims_years_agent": {"items": [{"model": "IONIQ 5", "years_sold": "2021-2026", "generation_or_series": "NE", "confidence": "high", "sources": ["trim"]}]},
        "engines_fuel_power_agent": {"items": [{"model": "ioniq 5", "engine": "EV", "fuel_type": "electric", "power_hp": 217, "confidence": "high", "sources": ["engine"]}]},
        "transmission_drivetrain_performance_agent": {"items": [{"model": "Ioniq 5", "transmission": "single-speed", "drivetrain": "RWD", "confidence": "medium"}]},
        "dimensions_safety_equipment_agent": {"items": [{"model": "Ioniq 5", "body_type": "SUV", "seats": 5, "safety": "ADAS", "confidence": "medium"}]},
    }
    verifier = {"status": "success", "verified_models": [{"model": "Ioniq 5", "status": "verified", "confidence": "high", "issues": [], "source_strength": "official_israel"}]}
    result = app.run_final_builder_phase("key", normalized, technical, verifier, [], "Hyundai", "Israel", "2010-2026")
    model = result["parsed"]["models"][0]
    assert result["status"] == "success"
    assert result["finish_reason"] == "python_merge_success"
    assert model["engine"] == "EV"
    assert model["transmission"] == "single-speed"
    assert model["body_type"] == "SUV"
    assert model["verification_status"] == "verified"


def test_python_final_builder_works_when_verifier_partial_or_missing_model():
    normalized = {"canonical_models": [{"canonical_model_name": "i10", "confidence": "high"}, {"canonical_model_name": "i20", "confidence": "high"}]}
    verifier = {"status": "partial", "verified_models": [{"model": "i10", "status": "verified", "confidence": "high", "issues": []}], "needs_review": []}
    result = app.run_final_builder_phase("key", normalized, {}, verifier, [{"agent": "source_verifier", "error": "VERIFIER_CHUNK_FAILED"}], "Hyundai", "Israel", "2010-2026")
    models = {m["canonical_model_name"]: m for m in result["parsed"]["models"]}
    assert result["parsed"]["pipeline_quality"]["verifier"] == "partial"
    assert models["i20"]["verification_status"] == "partial"
    assert models["i20"]["verification_notes"] == "Verifier did not complete for this model"


def test_final_builder_never_returns_model_json_truncated():
    normalized = {"canonical_models": [{"canonical_model_name": f"m{i}"} for i in range(100)]}
    with patch("app.run_safe_agent", side_effect=AssertionError("no LLM")):
        result = app.run_final_builder_phase("key", normalized, {}, {}, [], "Hyundai", "Israel", "2010-2026")
    assert result.get("error") != "MODEL_JSON_TRUNCATED"
    assert len(result["parsed"]["models"]) == 100


def test_model_matching_uses_model_field_and_aliases():
    normalized = {"canonical_models": [{"canonical_model_name": "Santa Fe", "aliases": ["Santafe"]}]}
    technical = {"engines_fuel_power_agent": {"items": [{"model": "santafe", "engine": "2.5T", "confidence": "high"}]}}
    result = app.run_final_builder_phase("key", normalized, technical, {}, [], "Hyundai", "Israel", "2010-2026")
    assert result["parsed"]["models"][0]["engine"] == "2.5T"


def test_verifier_chunk_size_is_six():
    assert app.VERIFIER_MODEL_CHUNK_SIZE == 6


def test_verifier_issues_are_capped_to_two_and_120_chars():
    parsed = {"agent": "source_verifier", "verified_models": [{"model": "x", "status": "partial", "confidence": "low", "issues": ["a" * 130, "b", "c"], "source_strength": "unknown"}], "rejected_data_points": [], "needs_review": []}
    assert app.validate_verifier_schema(parsed) is None
    assert len(parsed["verified_models"][0]["issues"]) == 2
    assert len(parsed["verified_models"][0]["issues"][0]) == 120


def test_failed_verifier_chunks_are_preserved_as_compact_summaries():
    merged = app.merge_verifier_results([
        app.phase_result(status="failed", agent="source_verifier", error="MODEL_JSON_TRUNCATED", raw_preview="SECRET"),
        app.phase_result(status="success", agent="source_verifier", parsed={"agent": "source_verifier", "verified_models": [{"model": "i10"}], "rejected_data_points": [], "needs_review": []}),
    ])
    assert merged["status"] == "partial"
    assert merged["failed_chunks"] == [{"agent": "source_verifier", "error": "MODEL_JSON_TRUNCATED", "chunk_index": 0}]
    assert "SECRET" not in json.dumps(merged, ensure_ascii=False)


def test_ui_status_data_contains_python_merge_success():
    result = app.run_final_builder_phase("key", {"canonical_models": []}, {}, {}, [], "Hyundai", "Israel", "2010-2026")
    assert result["parsed"]["final_builder_method"] == "python_merge_success"

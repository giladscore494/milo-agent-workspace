import importlib.util
import sys
import types
from pathlib import Path

import pytest

from backend.engines.vehicle_catalog_v1 import core
from backend.engines.vehicle_catalog_v1.adapter import VehicleCatalogV1Adapter


@pytest.fixture(scope="module")
def legacy_app():
    sys.modules.setdefault("streamlit", types.SimpleNamespace())
    if "openai" not in sys.modules:
        sys.modules["openai"] = types.SimpleNamespace(OpenAI=object)
    path = Path("legacy/milo-streamlit-v1/app.py")
    spec = importlib.util.spec_from_file_location("legacy_milo_app_for_parity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_preserved_numeric_limits_and_model_settings():
    assert core.MOONSHOT_BASE_URL == "https://api.moonshot.ai/v1"
    assert core.KIMI_MODEL == "kimi-k2.6"
    assert core.SEARCH_TEMPERATURE == 0.6
    assert core.CONSOLIDATION_TEMPERATURE == 0.6
    assert core.MAX_TOOL_ROUNDS == 15
    assert core.MAX_PARALLEL_KIMI_CALLS == 2
    assert core.API_CONCURRENCY_RETRY_DELAY_SECONDS == 2
    assert core.API_CONCURRENCY_MAX_RETRIES == 2
    assert core.TECHNICAL_MODEL_CHUNK_SIZE == 4
    assert core.VERIFIER_MODEL_CHUNK_SIZE == 6


def test_new_engine_core_does_not_import_streamlit():
    text = Path("backend/engines/vehicle_catalog_v1/core.py").read_text()
    assert "import streamlit" not in text


def test_normalization_parity_against_legacy(legacy_app):
    values = ["Hyundai i20 N Line", "איוניק 5", "Tucson", "Grand i10"]
    assert [core.normalize_model_name(v) for v in values] == [legacy_app.normalize_model_name(v) for v in values]
    assert [core.normalize_model_key(v) for v in values] == [legacy_app.normalize_model_key(v) for v in values]


def test_discovery_merge_parity_against_legacy(legacy_app):
    discovery = [
        {"agent": "current_official_lineup_agent", "status": "success", "parsed": {"models": [{"name": "Tucson", "model_name_he": "טוסון", "sources": ["official"], "evidence": "listed"}, {"name": "N Line", "sources": ["bad"]}]}},
        {"agent": "historical_used_market_agent", "status": "failed", "error": "boom"},
        {"agent": "ev_hybrid_edge_cases_agent", "status": "success", "parsed": {"models": [{"name": "Tucson", "sources": ["used"]}, {"name": "IONIQ 5", "sources": ["ev"]}]}},
    ]
    assert core.merge_discovery_candidates(discovery) == legacy_app.merge_discovery_candidates(discovery)


def test_validation_parity_against_legacy(legacy_app):
    payload = {"agent": "normalizer_deduper", "canonical_models": [{"model_name": "Tucson", "model_he": "טוסון", "sources": ["s"]}], "rejected_items": [], "needs_review": []}
    assert core.validate_normalizer_schema(payload.copy()) == legacy_app.validate_normalizer_schema(payload.copy())
    item_payload = {"agent": "trims_years_agent", "items": [{"model_name": "Tucson", "confidence": "high", "sources": ["s"], "notes": "n"}], "missing_data": [], "extra_candidate_models": []}
    assert core.validate_items_schema(item_payload.copy()) == legacy_app.validate_items_schema(item_payload.copy())
    verifier_payload = {"agent": "source_verifier", "verified_models": [{"model_name": "Tucson", "status": "verified", "issues": ["global source"]}], "rejected_data_points": [], "needs_review": []}
    assert core.validate_verifier_schema(verifier_payload.copy()) == legacy_app.validate_verifier_schema(verifier_payload.copy())


def test_chunk_verifier_and_final_merge_parity_against_legacy(legacy_app):
    chunk_results = [
        {"agent": "trims_years_agent", "status": "success", "parsed": {"agent": "trims_years_agent", "items": [{"model_name": "Tucson", "confidence": "high", "sources": ["s"]}], "missing_data": [], "extra_candidate_models": []}},
        {"agent": "trims_years_agent", "status": "failed", "error": "MODEL_INVALID_JSON", "message": "bad"},
    ]
    assert core.merge_chunk_results("trims_years_agent", chunk_results) == legacy_app.merge_chunk_results("trims_years_agent", chunk_results)
    verifier_chunks = [
        {"agent": "source_verifier", "status": "success", "parsed": {"agent": "source_verifier", "verified_models": [{"model_name": "Tucson", "status": "verified"}], "rejected_data_points": [], "needs_review": []}},
        {"agent": "source_verifier", "status": "failed", "error": "MODEL_INVALID_JSON", "message": "bad"},
    ]
    assert core.merge_verifier_results(verifier_chunks) == legacy_app.merge_verifier_results(verifier_chunks)
    normalizer = {"canonical_models": [{"model_name": "Tucson", "model_name_he": "טוסון", "sources": ["official"]}]}
    technical = {"trims_years_agent": {"items": [{"model_name": "Tucson", "confidence": "high", "sources": ["tech"], "notes": "trim"}]}}
    verifier = {"status": "success", "verified_models": [{"model_name": "Tucson", "status": "verified"}], "needs_review": []}
    args = (normalizer, technical, verifier, [{"agent": "x", "error": "e"}], "Hyundai", "Israel", "2010 to June 2026")
    assert core.build_final_json_python(*args) == legacy_app.build_final_json_python(*args)


def test_adapter_uses_fake_client_without_live_calls(monkeypatch):
    calls = []
    def fake_run(self, config):
        calls.append(config)
        return {"status": "partial_success", "result": {"models": []}}
    monkeypatch.setattr("backend.engines.vehicle_catalog_v1.engine.VehicleCatalogEngine.run", fake_run)
    result = VehicleCatalogV1Adapter(model_client_factory=lambda *_: object(), sleep_fn=lambda _: None).run({"input": {"manufacturer": "Hyundai"}})
    assert result["status"] == "partial_success"
    assert calls[0].manufacturer == "Hyundai"

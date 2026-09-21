import importlib.util
import inspect
import sys
import types
from pathlib import Path

import pytest

from backend import provider_authority, provider_scheduler, standalone_search
from backend.engines.vehicle_catalog_v1 import core
from backend.worker import main as worker_main
from backend.engines.vehicle_catalog_v1.adapter import VehicleCatalogV1Adapter


@pytest.fixture(scope="module")
def legacy_app():
    """The immutable Streamlit reference, imported for parity assertions.

    The stubs it needs are REMOVED again afterwards. Leaving them installed
    made `openai` resolve to `SimpleNamespace(OpenAI=object)` for the rest of
    the session, so any later test that really builds a provider client got
    `TypeError: object() takes no arguments` -- a failure with nothing to do
    with the test reporting it, and one that appears or vanishes with the
    selection, because the stub is only installed when nothing imported the
    real package first.
    """
    installed: list[str] = []
    for name, stub in (("streamlit", types.SimpleNamespace()),
                       ("openai", types.SimpleNamespace(OpenAI=object))):
        if name not in sys.modules:
            sys.modules[name] = stub
            installed.append(name)
    path = Path("legacy/milo-streamlit-v1/app.py")
    spec = importlib.util.spec_from_file_location("legacy_milo_app_for_parity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        for name in (*installed, spec.name):
            sys.modules.pop(name, None)


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


def test_the_legacy_host_constant_is_a_descriptor_not_an_authority():
    """`MOONSHOT_BASE_URL` may exist. It may not DECIDE anything.

    The constant above is a compatibility export the preserved-engine parity
    contract carries. The defect it used to be was different: V1 read its own
    hard-coded host while V2 chat and standalone search honoured
    MILO_MODEL_BASE_URL, so one deployment could address two providers and
    nothing could tell. Keeping the name is fine; keeping a SECOND
    configurable host is not.
    """
    # It is bound to the canonical default, so it cannot drift from it.
    assert core.MOONSHOT_BASE_URL is provider_authority.DEFAULT_PROVIDER_BASE_URL
    # V1's own client construction resolves the canonical function instead.
    v1_client = inspect.getsource(core._provider_client)
    assert "provider_base_url()" in v1_client
    assert "MOONSHOT_BASE_URL" not in v1_client

    # And no runtime module anywhere reads the descriptor. The sweep is over
    # backend/ rather than this engine alone, because a second authority is
    # just as harmful imported from somewhere else.
    readers = []
    for path in Path("backend").rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "MOONSHOT_BASE_URL" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue          # the descriptor's own explanation
            if stripped.startswith("MOONSHOT_BASE_URL = "):
                continue          # the descriptor itself
            readers.append(f"{path}:{number}: {stripped}")
    assert not readers, f"the legacy host descriptor is being read at runtime: {readers}"


def test_one_env_knob_moves_v1_chat_v2_chat_and_search_together(monkeypatch):
    """The split-brain configuration this PR removed cannot come back.

    Setting MILO_MODEL_BASE_URL must move ALL THREE provider paths, and the
    legacy descriptor must stay exactly where it was -- proving it is inert
    rather than a second knob that quietly disagrees with the first.
    """
    configured = "https://provider-proxy.example/v1"
    monkeypatch.setenv("MILO_MODEL_BASE_URL", configured + "/")

    # V1 chat: capture the base URL its client construction really passes.
    scheduler = provider_scheduler.ProviderScheduler(
        provider_scheduler.ProviderLimitsConfig())
    seen: list[str] = []
    monkeypatch.setattr(core, "PROVIDER_SCHEDULER", scheduler)
    monkeypatch.setattr(core, "PROVIDER_ADAPTER",
                        provider_authority.ProviderAdapter(scheduler))
    monkeypatch.setattr(core, "MODEL_CLIENT_FACTORY",
                        lambda api_key, base_url: seen.append(base_url) or object())
    core._provider_client("k")
    assert seen == [configured], "V1 chat did not follow the deployment knob"

    # Standalone search resolves the same value...
    assert standalone_search.search_base_url() == configured
    # ...and V2's gateway is wired from the same resolver rather than its own.
    worker_source = inspect.getsource(worker_main.execute_run)
    assert worker_source.count("base_url=provider_base_url()") == 2, (
        "worker chat/search wiring no longer both read the canonical resolver")
    assert provider_authority.provider_base_url() == configured

    # The descriptor did NOT move, because it decides nothing.
    assert core.MOONSHOT_BASE_URL == "https://api.moonshot.ai/v1"


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

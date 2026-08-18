"""Deterministic Israel-market source policy and discovery-context tests.

Regression fixtures mirror the Attempt-6 source-quality failure: foreign
dealer/market domains (hyundaiofkennesaw.com, hyundaiusa.com, Vietnamese and
Saudi portals) must never establish official Israeli-market presence, while
credible Israeli/government/importer evidence passes the stronger policy.
No network and no provider calls."""

from __future__ import annotations

import pytest

from backend.engines.vehicle_catalog_v1 import core
from backend.engines.vehicle_catalog_v1.engine import VehicleCatalogEngine, VehicleCatalogRunConfig
from backend.engines.vehicle_catalog_v1.source_policy import (
    FOREIGN_ONLY_NOTE,
    ISRAEL_MARKET_CLASSES,
    SOURCE_CLASS_RANK,
    apply_israel_source_policy,
    classify_source,
    evaluate_sources,
    market_requires_israel_policy,
)


# -- deterministic URL classification ----------------------------------------


@pytest.mark.parametrize("url,manufacturer,expected", [
    # Attempt-6 regression domains: foreign, never Israeli evidence.
    ("https://www.hyundaiofkennesaw.com/inventory", "Hyundai", "foreign_market"),
    ("https://www.hyundaiusa.com/us/en/vehicles", "Hyundai", "foreign_market"),
    ("https://hyundai.com.vn/models", "Hyundai", "foreign_market"),
    ("https://hyundai.com.sa/ar", "Hyundai", "foreign_market"),
    ("https://xehay.vn/hyundai-accent", "Hyundai", "foreign_market"),
    # Global official is distinct from official Israel.
    ("https://www.hyundai.com/worldwide/en", "Hyundai", "global_official"),
    ("https://global.hyundai.com/models", "Hyundai", "global_official"),
    # Credible Israeli evidence.
    ("https://www.gov.il/he/departments/ministry_of_transport", "Hyundai", "government"),
    ("https://data.gov.il/dataset/private-vehicles", "Hyundai", "government"),
    ("https://www.hyundai.co.il/models", "Hyundai", "official_israel"),
    ("https://www.icar.co.il/hyundai", "Hyundai", "israeli_auto_portal"),
    ("https://www.yad2.co.il/vehicles/cars?manufacturer=hyundai", "Hyundai", "used_market"),
    # Weak / malformed evidence.
    ("https://random-blog.com/cars", "Hyundai", "weak"),
    ("", "Hyundai", "weak"),
    (None, "Hyundai", "weak"),
    ("not a url at all ::::", "Hyundai", "weak"),
])
def test_classify_source_matrix(url, manufacturer, expected):
    assert classify_source(url, manufacturer) == expected


def test_policy_is_generic_not_hyundai_specific():
    assert classify_source("https://www.toyota.co.il/corolla", "Toyota") == "official_israel"
    assert classify_source("https://toyota.com.vn/vios", "Toyota") == "foreign_market"
    assert classify_source("https://www.toyota.com/models", "Toyota") == "global_official"
    assert classify_source("https://www.kia.co.il/", "Kia") == "official_israel"
    # A different manufacturer's Israeli domain is still Israeli evidence,
    # just not the official channel of THIS manufacturer.
    assert classify_source("https://www.toyota.co.il/", "Hyundai") == "israeli_auto_portal"


def test_ranking_and_israel_classes_are_consistent():
    assert SOURCE_CLASS_RANK["government"] > SOURCE_CLASS_RANK["official_israel"] > SOURCE_CLASS_RANK["global_official"] > SOURCE_CLASS_RANK["foreign_market"] >= SOURCE_CLASS_RANK["weak"] + 1
    assert "global_official" not in ISRAEL_MARKET_CLASSES
    assert "foreign_market" not in ISRAEL_MARKET_CLASSES
    assert "government" in ISRAEL_MARKET_CLASSES


def test_evaluate_sources_foreign_only_vs_mixed():
    foreign_only = evaluate_sources(["https://hyundaiusa.com/x", "https://hyundaiofkennesaw.com/y"], "Hyundai")
    assert foreign_only["israel_market_evidence"] is False
    assert foreign_only["best_source_class"] == "foreign_market"
    mixed = evaluate_sources(["https://hyundaiusa.com/x", "https://www.hyundai.co.il/models"], "Hyundai")
    assert mixed["israel_market_evidence"] is True
    assert mixed["best_source_class"] == "official_israel"
    assert evaluate_sources([], "Hyundai")["israel_market_evidence"] is False


# -- discovery prompt context --------------------------------------------------


@pytest.mark.parametrize("retry", [False, True])
def test_israel_discovery_context_is_injected_into_every_discovery_prompt(retry):
    for agent in core.DISCOVERY_AGENTS:
        messages = core.discovery_prompt(agent, "Hyundai", "Israel", "2010 to June 2026", retry=retry)
        system = messages[0]["content"]
        assert core.ISRAEL_DISCOVERY_CONTEXT.strip() in system
        # The mandatory web-search instruction is not duplicated or lost.
        assert system.count("You MUST call the $web_search tool") == 1


def test_non_israel_market_does_not_get_israel_context():
    messages = core.discovery_prompt(core.DISCOVERY_AGENTS[0], "Hyundai", "Germany", "2010", retry=False)
    assert core.ISRAEL_DISCOVERY_CONTEXT.strip() not in messages[0]["content"]


# -- final-output policy enforcement ------------------------------------------


def make_final(models):
    return {
        "manufacturer": "Hyundai",
        "market": "Israel",
        "period": "2010 to June 2026",
        "status": "complete",
        "models": models,
        "needs_review": [m for m in models if m.get("verification_status") in {"partial", "needs_review"}],
        "rejected": [m for m in models if m.get("verification_status") == "rejected"],
        "failed_agents": [],
        "token_usage": {},
    }


def foreign_only_model(name="Palisade"):
    return {
        "canonical_model_name": name,
        "confidence": "high",
        "sources": ["https://www.hyundaiofkennesaw.com/palisade", "https://www.hyundaiusa.com/palisade"],
        "verification_status": "verified",
        "verification_notes": None,
    }


def israeli_model(name="Tucson"):
    return {
        "canonical_model_name": name,
        "confidence": "high",
        "sources": ["https://www.hyundai.co.il/tucson", "https://www.gov.il/vehicle-data"],
        "verification_status": "verified",
        "verification_notes": None,
    }


def test_foreign_only_evidence_is_downgraded_but_preserved():
    final = apply_israel_source_policy(make_final([foreign_only_model()]), "Hyundai")
    model = final["models"][0]
    assert model["verification_status"] == "needs_review"
    assert model["confidence"] == "medium"  # never silently keeps high
    assert FOREIGN_ONLY_NOTE in model["verification_notes"]
    assert model["source_policy"]["israel_market_evidence"] is False
    assert model in final["needs_review"]  # preserved, surfaced for review
    assert len(final["models"]) == 1  # never hidden or removed


def test_credible_israel_evidence_passes_the_stronger_policy():
    final = apply_israel_source_policy(make_final([israeli_model()]), "Hyundai")
    model = final["models"][0]
    assert model["verification_status"] == "verified"
    assert model["confidence"] == "high"
    assert model["source_policy"]["israel_market_evidence"] is True
    assert model["source_policy"]["best_source_class"] == "government"
    assert final["needs_review"] == []


def test_mixed_foreign_and_israeli_evidence_is_not_downgraded():
    model = foreign_only_model()
    model["sources"].append("https://www.yad2.co.il/vehicles/cars?model=palisade")
    final = apply_israel_source_policy(make_final([model]), "Hyundai")
    assert final["models"][0]["verification_status"] == "verified"
    assert final["models"][0]["confidence"] == "high"


def test_global_official_alone_is_not_israeli_evidence():
    model = foreign_only_model()
    model["sources"] = ["https://www.hyundai.com/worldwide/en/vehicles"]
    final = apply_israel_source_policy(make_final([model]), "Hyundai")
    assert final["models"][0]["verification_status"] == "needs_review"
    assert final["models"][0]["source_policy"]["best_source_class"] == "global_official"


def test_sourceless_candidate_is_flagged_not_dropped():
    model = foreign_only_model()
    model["sources"] = []
    final = apply_israel_source_policy(make_final([model]), "Hyundai")
    assert final["models"][0]["verification_status"] == "needs_review"
    assert len(final["models"]) == 1


def test_rejected_models_stay_rejected():
    model = foreign_only_model()
    model["verification_status"] = "rejected"
    final = apply_israel_source_policy(make_final([model]), "Hyundai")
    assert final["models"][0]["verification_status"] == "rejected"
    assert final["models"][0] in final["rejected"]


def test_policy_is_idempotent():
    final = apply_israel_source_policy(make_final([foreign_only_model()]), "Hyundai")
    again = apply_israel_source_policy(final, "Hyundai")
    model = again["models"][0]
    assert model["verification_notes"].count(FOREIGN_ONLY_NOTE) == 1
    assert model["confidence"] == "medium"


def test_policy_generic_for_other_manufacturers():
    model = {
        "canonical_model_name": "Corolla",
        "confidence": "high",
        "sources": ["https://toyota.com.vn/corolla"],
        "verification_status": "verified",
        "verification_notes": None,
    }
    final = apply_israel_source_policy(make_final([model]), "Toyota")
    assert final["models"][0]["verification_status"] == "needs_review"


# -- engine hook ---------------------------------------------------------------


def test_engine_applies_policy_only_for_israel_market():
    engine = VehicleCatalogEngine()
    israel_config = VehicleCatalogRunConfig(api_key="k", market="Israel")
    other_config = VehicleCatalogRunConfig(api_key="k", market="Germany")

    final = {"status": "success", "parsed": make_final([foreign_only_model()])}
    engine._apply_source_policy(final, israel_config)
    assert final["parsed"]["models"][0]["verification_status"] == "needs_review"

    untouched = {"status": "success", "parsed": make_final([foreign_only_model()])}
    engine._apply_source_policy(untouched, other_config)
    assert untouched["parsed"]["models"][0]["verification_status"] == "verified"

    assert market_requires_israel_policy("Israel") and market_requires_israel_policy("israel il")
    assert not market_requires_israel_policy("Germany")

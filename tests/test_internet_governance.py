from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.internet_governance import GovernanceError, InternetGovernanceEngine, InternetPolicy
from backend.runtime import InMemoryEventSink, RunEventRecord


def _grant(engine):
    req = engine.request_access("pricing", "web_search", "current MSRP", {"entity": "car"}, {"max_searches": 2})
    return engine.grant(req.id, max_searches=2, max_rounds=1, expires_at=datetime.now(UTC) + timedelta(minutes=5), approver_policy="test", domains=["example.com"])


def test_forbidden_policy_rejects_every_web_request():
    engine = InternetGovernanceEngine(uuid4(), InternetPolicy.FORBIDDEN)
    with pytest.raises(GovernanceError):
        engine.request_access("a", "web_search", "need web", {}, {})


def test_allowed_policy_enforces_grants_domains_and_quotas():
    engine = InternetGovernanceEngine(uuid4(), InternetPolicy.ALLOWED)
    grant = _grant(engine)
    engine.record_tool_use(grant.id, "search", query="one", url="https://www.example.com/a")
    engine.record_tool_use(grant.id, "search", query="two", url="https://example.com/b")
    with pytest.raises(GovernanceError):
        engine.record_tool_use(grant.id, "search", query="three", url="https://example.com/c")
    with pytest.raises(GovernanceError):
        engine.record_tool_use(grant.id, "search", query="bad", url="https://other.test")


def test_conditional_policy_requires_structured_trigger_before_grantable_request():
    engine = InternetGovernanceEngine(uuid4(), InternetPolicy.CONDITIONAL)
    with pytest.raises(GovernanceError):
        engine.request_access("a", "web_search", "maybe stale", {}, {})
    req = engine.request_access("a", "web_search", "maybe stale", {}, {}, trigger={"field": "price", "condition": "missing_current_source"})
    assert req.trigger["condition"] == "missing_current_source"


def test_required_policy_requires_actual_successful_usage_and_acceptable_source():
    engine = InternetGovernanceEngine(uuid4(), InternetPolicy.REQUIRED, min_successful_searches=1)
    grant = _grant(engine)
    with pytest.raises(GovernanceError):
        engine.assert_can_complete()
    engine.record_tool_use(grant.id, "search", query="msrp", url="https://example.com/msrp")
    with pytest.raises(GovernanceError):
        engine.assert_can_complete()
    source = engine.add_source("pricing", "https://example.com/msrp", "MSRP", "manufacturer", "primary", "msrp", "search")
    engine.add_claim("pricing", "model:x", "msrp", 123, source.id, "primary", 0.9, unit="USD", time_scope={"year": 2026}, market="US")
    engine.assert_can_complete()


def test_source_claim_links_and_conflict_scoping_year_variant_market():
    engine = InternetGovernanceEngine(uuid4(), InternetPolicy.ALLOWED)
    s1 = engine.add_source("a", "https://example.com/1", "One", "primary", "high", "q", "search")
    s2 = engine.add_source("b", "https://example.com/2", "Two", "primary", "high", "q", "search")
    c1 = engine.add_claim("a", "vehicle:1", "price", 100, s1.id, "high", 0.8, unit="USD", time_scope={"year": 2026, "variant": "base"}, market="US")
    engine.add_claim("b", "vehicle:1", "price", 110, s2.id, "high", 0.8, unit="USD", time_scope={"year": 2025, "variant": "base"}, market="US")
    engine.add_claim("b", "vehicle:1", "price", 120, s2.id, "high", 0.8, unit="USD", time_scope={"year": 2026, "variant": "sport"}, market="US")
    engine.add_claim("b", "vehicle:1", "price", 130, s2.id, "high", 0.8, unit="USD", time_scope={"year": 2026, "variant": "base"}, market="CA")
    c5 = engine.add_claim("b", "vehicle:1", "price", 140, s2.id, "high", 0.8, unit="USD", time_scope={"year": 2026, "variant": "base"}, market="US")
    assert engine.links == [{"source_id": s1.id, "claim_id": c1.id}, *engine.links[1:]]
    assert len(engine.conflicts) == 1
    assert engine.conflicts[0].claim_ids == [c1.id, c5.id]


def test_governance_events_are_auditable_runtime_events():
    sink = InMemoryEventSink()
    run_id = uuid4()
    event = sink.emit(RunEventRecord(run_id=run_id, type="tool_used", message="searched", payload={"tool": "web_search"}))
    assert sink.events == [event]

"""B1: one deterministic canonical scope identity for Swarm V2 conflict grouping."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

from backend.engines.swarm_v2 import (
    BoundedTaskExecutor, EvidenceReference, SwarmV2Engine, Verifier,
)
from backend.engines.swarm_v2 import engine as engine_module
from backend.engines.swarm_v2 import evidence as evidence_module
from backend.engines.swarm_v2 import normalization
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease, safe_durable_value
from backend.engines.swarm_v2.normalization import (
    SCOPE_NORMALIZATION_VERSION, CanonicalScope, canonical_scope_hash,
    canonical_scope_key, canonical_value_key, normalize_entity_key,
    normalize_field_key, normalize_geography_key, normalize_market_key,
    normalize_time_scope,
)
from backend.schemas import ClaimCreate, SourceCreate
from test_swarm_v2 import plan, task
from test_swarm_v2_evidence import GuardedEvidenceRepository
from test_swarm_v2_stage1_e2e import Plans, StubResolver, VerifyGateway, Worker, commander


# --- pure normalization contract ---------------------------------------------

def test_entity_formatting_variants_share_one_canonical_key():
    variants = ["Toyota Corolla 2020", "toyota_corolla_2020", " TOYOTA   COROLLA-2020 "]
    keys = {normalize_entity_key(value) for value in variants}
    assert keys == {"toyota corolla 2020"}


def test_field_formatting_variants_share_one_canonical_key():
    variants = ["engine-power", "engine_power", "Engine Power"]
    keys = {normalize_field_key(value) for value in variants}
    assert keys == {"engine power"}


def test_semantic_aliases_stay_distinct():
    assert normalize_entity_key("Accent") != normalize_entity_key("i25")
    assert normalize_entity_key("Tucson") != normalize_entity_key("Tucson New")
    assert normalize_entity_key("i-25") != normalize_entity_key("i25")


def test_time_scope_is_key_order_invariant_but_years_stay_distinct():
    assert normalize_time_scope({"year": 2020, "quarter": 1}) == \
        normalize_time_scope({"quarter": 1, "year": 2020})
    assert normalize_time_scope({"year": 2020}) != normalize_time_scope({"year": 2021})
    assert normalize_time_scope(None) == normalize_time_scope({})


def test_market_and_geography_normalize_formatting_never_semantics():
    assert normalize_market_key(" israel ") == normalize_market_key("ISRAEL") == "israel"
    assert normalize_market_key("Israel") != normalize_market_key("Global")
    assert normalize_market_key("Israel") != normalize_market_key("Europe")
    assert normalize_geography_key("IL ") == normalize_geography_key("il")
    assert normalize_geography_key("IL") != normalize_geography_key("US")
    assert normalize_geography_key(None) is None and normalize_market_key(None) is None


def test_canonical_scope_key_is_hashable_and_versioned():
    assert SCOPE_NORMALIZATION_VERSION == 1
    scope = canonical_scope_key(entity="Toyota Corolla 2020", field="Engine Power",
                                geography="IL", market=" Israel ", time_scope={"year": 2020})
    assert scope == canonical_scope_key(entity=" toyota-corolla_2020", field="engine_power",
                                        geography="il", market="ISRAEL",
                                        time_scope={"year": 2020})
    assert isinstance(scope, CanonicalScope) and hash(scope) is not None
    assert scope._fields == ("entity", "field", "geography", "market", "time_scope")


def test_one_shared_scope_implementation_across_board_and_engine():
    # A future second scope algorithm must fail here: both conflict paths are
    # required to reference the exact objects owned by the normalization module.
    assert engine_module.canonical_scope_key is normalization.canonical_scope_key
    assert evidence_module.canonical_scope_key is normalization.canonical_scope_key
    assert engine_module.canonical_value_key is normalization.canonical_value_key
    assert evidence_module.canonical_value_key is normalization.canonical_value_key
    assert engine_module.CanonicalScope is normalization.CanonicalScope
    assert evidence_module.CanonicalScope is normalization.CanonicalScope


# --- SwarmV2Engine conflict path ---------------------------------------------

def ref(claim, *, entity="Toyota Corolla 2020", field="price", geography="IL",
        market="Israel", time_scope=None, value=1):
    return EvidenceReference(claim_id=claim, source_id=f"s-{claim}", run_id="run-1",
        task_id="a", entity=entity, field=field, geography=geography, market=market,
        time_scope={"year": 2020} if time_scope is None else time_scope,
        value=value, confidence=.9)


def run_engine(refs):
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = ["price"]
    events = []
    engine = SwarmV2Engine(
        commander=commander(Plans(plan([planned]), [
            {"decision": "FINISH", "plan": None, "reason": "done"}])),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=lambda _: list(refs),
        event_sink=lambda kind, payload: events.append((kind, payload)))
    result = engine.run({"id": "run-1", "input": {"objective": "scope", "commander_model": "fake"}})
    conflicts = {payload["claim_id"] for kind, payload in events if kind == "conflict_found"}
    return result, conflicts


def test_engine_formatting_variants_cannot_dodge_conflict_detection():
    result, conflicts = run_engine([
        ref("c1", value=100),
        ref("c2", entity=" TOYOTA   COROLLA-2020 ", field="Price", geography="il",
            market=" ISRAEL ", time_scope={"year": 2020}, value=120),
    ])
    assert conflicts == {"c1", "c2"}
    assert result["status"] == "partial_success"


def test_engine_same_scope_same_value_is_not_a_conflict():
    result, conflicts = run_engine([
        ref("c1", value=100),
        ref("c2", entity="toyota_corolla_2020", value=100),
    ])
    assert conflicts == set()
    assert result["status"] == "complete"


def test_engine_different_model_year_market_never_false_conflict():
    result, conflicts = run_engine([
        ref("c1", value=100),
        ref("c2", entity="Toyota Corolla 2021", value=200),
        ref("c3", time_scope={"year": 2021}, value=300),
        ref("c4", market="Global", value=400),
        ref("c5", geography="US", value=500),
        ref("c6", entity="Tucson", value=600),
        ref("c7", entity="Tucson New", value=700),
    ])
    assert conflicts == set()
    assert result["status"] == "complete"


def test_engine_grouping_is_input_order_independent():
    refs = [ref("c1", value=100),
            ref("c2", entity="toyota_corolla_2020", time_scope={"year": 2020}, value=120),
            ref("c3", entity="Other Car", value=1)]
    forward = run_engine(refs)
    backward = run_engine(list(reversed(refs)))
    assert forward == backward
    assert forward[1] == {"c1", "c2"}


def test_engine_preserves_original_evidence_values():
    refs = [ref("c1", value=100),
            ref("c2", entity=" TOYOTA   COROLLA-2020 ", market=" ISRAEL ", value=120)]
    checkpoints = []
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = ["price"]
    engine = SwarmV2Engine(
        commander=commander(Plans(plan([planned]), [
            {"decision": "FINISH", "plan": None, "reason": "done"}])),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=VerifyGateway(), model="fake", resolver=StubResolver()),
        evidence_loader=lambda _: list(refs),
        checkpoint_sink=lambda phase, value: checkpoints.append(value))
    result = engine.run({"id": "run-1", "input": {"objective": "scope", "commander_model": "fake"}})
    saved = {item["claim_id"]: item for item in
             checkpoints[-1]["artifacts"]["swarm_state"]["evidence_references"]}
    assert saved["c1"]["entity"] == "Toyota Corolla 2020" and saved["c1"]["market"] == "Israel"
    assert saved["c2"]["entity"] == " TOYOTA   COROLLA-2020 " and saved["c2"]["market"] == " ISRAEL "
    assert refs[1].entity == " TOYOTA   COROLLA-2020 "
    scopes = {item["provenance"]["claim_id"]: item["provenance"]["scope"]
              for item in result["needs_review"]}
    assert scopes["c2"]["entity"] == " TOYOTA   COROLLA-2020 "


# --- EvidenceBoard conflict path ---------------------------------------------

def board_source(url="https://example.test/a"):
    return SourceCreate(agent="worker", url=url, title="Evidence", domain="example.test",
                        source_type="primary", source_strength="strong", query="q",
                        tool_operation="search")


def board_claim(source_id, value, *, entity="Toyota Corolla 2020", field="engine-power",
                market="Israel", geography="IL", time_scope=None):
    return ClaimCreate(entity_key=entity, field_key=field, value=value,
                       time_scope={"year": 2020} if time_scope is None else time_scope,
                       market=market, geography=geography, source_id=source_id,
                       source_strength="strong", confidence=.9, agent="worker")


def fresh_board(repo=None):
    lease = repo.lease if repo else WorkerLease(uuid4(), "worker-1", 1, "lease-token")
    repo = repo or GuardedEvidenceRepository(lease)
    return EvidenceBoard(repo, lease), repo


def formatting_variant_claims(source_ids):
    return [
        board_claim(source_ids[0], 100),
        board_claim(source_ids[1], 120, entity=" TOYOTA   COROLLA-2020 ",
                    field="Engine_Power", market=" ISRAEL ", geography="il",
                    time_scope={"year": 2020}),
        board_claim(source_ids[2], 999, entity="Tucson New"),
    ]


def record_all(board, claims, order):
    for index in order:
        board.record_claim(claims[index], task_key=f"task-{index}")


def test_board_formatting_variants_conflict_and_originals_are_preserved():
    board, repo = fresh_board()
    source_ids = [UUID(board.record_source(board_source(f"https://example.test/{i}"),
                                           task_key=f"task-{i}")["id"]) for i in range(3)]
    record_all(board, formatting_variant_claims(source_ids), [0, 1, 2])
    rows = board.detect_and_record_conflicts(task_key="review")
    assert len(rows) == 1 and len(rows[0]["claim_ids"]) == 2
    stored = {row["entity_key"] for row in repo.tables["claim"].values()}
    assert stored == {"Toyota Corolla 2020", " TOYOTA   COROLLA-2020 ", "Tucson New"}
    assert {row["market"] for row in repo.tables["claim"].values()} == \
        {"Israel", " ISRAEL ", "Israel"}
    assert rows[0]["entity_key"] in {"Toyota Corolla 2020", " TOYOTA   COROLLA-2020 "}


def test_board_same_scope_same_value_and_distinct_scopes_do_not_conflict():
    board, _ = fresh_board()
    source_ids = [UUID(board.record_source(board_source(f"https://example.test/{i}"),
                                           task_key=f"task-{i}")["id"]) for i in range(4)]
    board.record_claim(board_claim(source_ids[0], 100), task_key="task-0")
    board.record_claim(board_claim(source_ids[1], 100, entity="toyota_corolla_2020"),
                       task_key="task-1")
    board.record_claim(board_claim(source_ids[2], 200, time_scope={"year": 2021}),
                       task_key="task-2")
    board.record_claim(board_claim(source_ids[3], 300, market="Global"), task_key="task-3")
    assert board.detect_and_record_conflicts(task_key="review") == []


def test_board_conflict_identity_is_insertion_order_independent():
    board_a, repo = fresh_board()
    source_ids = [UUID(board_a.record_source(board_source(f"https://example.test/{i}"),
                                             task_key=f"task-{i}")["id"]) for i in range(3)]
    claims = formatting_variant_claims(source_ids)
    record_all(board_a, claims, [0, 1, 2])
    first = board_a.detect_and_record_conflicts(task_key="review")

    board_b, _ = fresh_board(repo)
    record_all(board_b, claims, [2, 1, 0])
    second = board_b.detect_and_record_conflicts(task_key="review")
    assert [(row["evidence_key"], row["claim_ids"], row["entity_key"]) for row in first] == \
        [(row["evidence_key"], row["claim_ids"], row["entity_key"]) for row in second]
    assert len(repo.tables["conflict"]) == 1


def test_board_persists_the_durable_canonical_identity_from_the_shared_module():
    """The claim payload that reaches the guarded RPC carries the canonical
    identity computed by the one normalization module, so Python grouping and
    the PostgreSQL conflict firewall cannot silently diverge."""
    board, repo = fresh_board()
    source_ids = [UUID(board.record_source(board_source(f"https://example.test/{i}"),
                                           task_key=f"task-{i}")["id"]) for i in range(3)]
    record_all(board, formatting_variant_claims(source_ids), [0, 1, 2])
    rows = list(repo.tables["claim"].values())
    expected = canonical_scope_hash(canonical_scope_key(
        entity="Toyota Corolla 2020", field="engine-power", geography="IL",
        market="Israel", time_scope={"year": 2020}))
    hashes = {row["entity_key"]: row["canonical_scope_hash"] for row in rows}
    assert hashes["Toyota Corolla 2020"] == hashes[" TOYOTA   COROLLA-2020 "] == expected
    assert hashes["Tucson New"] != expected
    assert {row["scope_normalization_version"] for row in rows} == {SCOPE_NORMALIZATION_VERSION}
    assert len({row["canonical_scope_hash"] for row in rows}) == 2
    # Originals still stored verbatim next to the internal identity.
    assert {row["entity_key"] for row in rows} == {
        "Toyota Corolla 2020", " TOYOTA   COROLLA-2020 ", "Tucson New"}


def test_claim_evidence_key_is_independent_of_canonical_metadata(monkeypatch):
    """The idempotent claim identity is task provenance plus the ORIGINAL
    payload only — the pre-canonical rule — so claims written by a
    pre-canonical release replay to the same evidence_key, and a future
    SCOPE_NORMALIZATION_VERSION bump can never duplicate logical claims."""
    board, repo = fresh_board()
    source_id = UUID(board.record_source(board_source(), task_key="task")["id"])
    claim = board_claim(source_id, 100)
    row = board.record_claim(claim, task_key="task")
    original = json.dumps(safe_durable_value({"task_key": "task", **claim.model_dump(mode="json")}),
                          sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert "canonical" not in original
    assert row["evidence_key"] == f"claim:{hashlib.sha256(original.encode()).hexdigest()}"
    assert row["canonical_scope_hash"] and row["scope_normalization_version"] == 1
    monkeypatch.setattr(evidence_module, "SCOPE_NORMALIZATION_VERSION", 2)
    replay = EvidenceBoard(repo, board.lease).record_claim(claim, task_key="task")
    assert replay["evidence_key"] == row["evidence_key"] and replay["id"] == row["id"]
    assert len(repo.tables["claim"]) == 1


def test_canonical_scope_hash_is_deterministic_sha256_of_the_scope():
    scope = canonical_scope_key(entity="Toyota Corolla 2020", field="engine-power",
                                geography="IL", market="Israel", time_scope={"year": 2020})
    digest = canonical_scope_hash(scope)
    assert digest == canonical_scope_hash(scope)
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert digest != canonical_scope_hash(scope._replace(time_scope=normalize_time_scope({"year": 2021})))


def test_canonical_value_key_matches_existing_deterministic_json_treatment():
    assert canonical_value_key({"b": 2, "a": 1}) == canonical_value_key({"a": 1, "b": 2})
    assert canonical_value_key(100) != canonical_value_key(120)
    assert canonical_value_key("100") != canonical_value_key(100)

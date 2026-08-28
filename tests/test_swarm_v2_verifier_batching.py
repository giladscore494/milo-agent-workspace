"""B4: deterministic, bounded, resumable Verifier batching.

These are the B4 guarantees, kept green on the B5 grounded payload: the
claim-count bound, the serialized-byte bound measured on the EXACT request,
deterministic partitioning, sequential execution, batch-local omission,
duplicate/foreign claim protections, checkpointing and resume.

Every model call in this module is an offline fake behind the existing
ModelGateway contract, and the resolver is an offline stub. No network, no
provider, no repository, no paid call.
"""
from __future__ import annotations

import json
import random
from copy import deepcopy

import pytest

from backend.engines.swarm_v2 import (
    MAX_VERIFIER_BATCH_JSON_BYTES, MAX_VERIFIER_CLAIMS_PER_BATCH,
    GROUNDED_VERDICT_REASONS, MAX_VERIFIER_EVIDENCE_CHARS_PER_BATCH,
    BoundedTaskExecutor, EvidenceReference,
    GroundedCandidate, RemainingBudget, SwarmState, SwarmV2Engine, Verifier,
    VerifierContractError, build_verifier_batches, parse_verifier_batch,
    plan_grounded_verification, serialize_verifier_candidates, verifier_payload_bytes,
)
from test_swarm_v2 import plan, task
from test_swarm_v2_stage1_e2e import Plans, StubResolver, Worker, commander

SENTINEL = "ZZ-RAW-VERIFIER-SENTINEL-7f3a"


def ref(index: int, *, value: object | None = None, supported: bool = True,
        task_id: str = "a", entity: str | None = None) -> EvidenceReference:
    """One evidence reference with a distinct scope, so it never conflicts."""
    claim_id = f"claim-{index:04d}"
    return EvidenceReference(claim_id=claim_id, source_id=f"source-{index:04d}",
        run_id="run-1", task_id=task_id, entity=entity or f"entity-{index:04d}",
        field="answer", value=f"value-{index:04d}" if value is None else value,
        confidence=0.9, supported=supported)


def refs(count: int, **kwargs: object) -> list[EvidenceReference]:
    return [ref(index, **kwargs) for index in range(count)]


def verifier(gateway, resolver=None) -> Verifier:
    """A Verifier whose ONLY route to durable evidence is an injected stub."""
    return Verifier(gateway=gateway, model="fake", resolver=resolver or StubResolver())


def grounded(items, resolver=None) -> list[GroundedCandidate]:
    """Join references to their stub source contexts, exactly as prepare does."""
    contexts = (resolver or StubResolver()).resolve(list(items))
    return [GroundedCandidate(reference=item, source=contexts[item.source_id])
            for item in items]


class RecordingGateway:
    """Offline stand-in for ModelGateway that records every verifier batch."""

    def __init__(self, responder=None, log: list | None = None):
        self.batches: list[list[str]] = []
        self.payloads: list[str] = []
        self.documents: list[dict] = []
        self.model_calls = 0
        self.max_in_flight = 0
        self._in_flight = 0
        self._responder = responder
        self._log = log if log is not None else []

    def call(self, **kwargs):
        assert kwargs["agent"] == "verifier" and kwargs["phase"] == "verification"
        assert kwargs["response_format"] == {"type": "json_object"}
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            content = kwargs["messages"][1]["content"]
            document = json.loads(content)
            claim_ids = [item["claim_id"] for item in document["claims"]]
            by_source = {source["source_id"]: [item["content_hash"] for item in source["fragments"]]
                         for source in document["sources"]}
            # The hashes a grounded `verified` verdict may legitimately cite:
            # only the fragments of that claim's OWN source.
            hashes = {claim["claim_id"]: by_source[claim["source_id"]]
                      for claim in document["claims"]}
            self.payloads.append(content)
            self.documents.append(document)
            self.batches.append(claim_ids)
            self.model_calls += 1
            self._log.append(("call", self.model_calls))
            if self._responder is not None:
                return self._responder(claim_ids, self.model_calls, hashes)
            return {"verdicts": [{"claim_id": claim_id, "verdict": "verified",
                                  "supporting_fragment_hashes": hashes[claim_id][:1]}
                                 for claim_id in claim_ids]}
        finally:
            self._in_flight -= 1

    def snapshot(self) -> dict[str, int]:
        return {"model_calls": self.model_calls}


# --- A. no candidate reaches a model call ------------------------------------

def test_all_deterministic_claims_consume_zero_verifier_model_calls():
    unsupported = [ref(0, supported=False), ref(1, supported=False)]
    conflicted = [ref(2, entity="shared"), ref(3, entity="shared")]
    gateway, resolver = RecordingGateway(), StubResolver()
    verdicts = verifier(gateway, resolver).verify(
        [*unsupported, *conflicted], conflict_claim_ids={"claim-0002", "claim-0003"})
    assert gateway.model_calls == 0
    # Q/R: a deterministically settled claim needs no grounding context at all.
    assert resolver.calls == []
    assert [(v.claim_id, v.verdict, v.reason) for v in verdicts] == [
        ("claim-0000", "rejected", "unsupported claim"),
        ("claim-0001", "rejected", "unsupported claim"),
        ("claim-0002", "needs_review", "unresolved conflict"),
        ("claim-0003", "needs_review", "unresolved conflict"),
    ]


def test_unsupported_wins_over_conflict_so_identity_is_never_duplicated():
    """Exactly one verdict per claim: an unsupported value is never surfaced
    for review just because its scope also conflicts."""
    items = [ref(0, supported=False, entity="shared"), ref(1, entity="shared")]
    gateway = RecordingGateway()
    verdicts = verifier(gateway).verify(
        items, conflict_claim_ids={"claim-0000", "claim-0001"})
    assert gateway.model_calls == 0
    assert len({v.claim_id for v in verdicts}) == len(verdicts) == 2
    assert (verdicts[0].verdict, verdicts[0].reason) == ("rejected", "unsupported claim")
    assert (verdicts[1].verdict, verdicts[1].reason) == ("needs_review", "unresolved conflict")


# --- B/C/D/E/N. bounded deterministic partitioning ---------------------------

def test_batch_constants_are_pinned():
    """B5 does not loosen either B4 bound to make room for evidence."""
    assert MAX_VERIFIER_CLAIMS_PER_BATCH == 25
    assert MAX_VERIFIER_BATCH_JSON_BYTES == 32_768
    assert MAX_VERIFIER_EVIDENCE_CHARS_PER_BATCH == 12_000


def test_one_small_batch_makes_exactly_one_call_with_the_exact_payload():
    items = refs(3)
    gateway, resolver = RecordingGateway(), StubResolver()
    verdicts = verifier(gateway, resolver).verify(items)
    assert gateway.model_calls == 1
    assert gateway.batches == [["claim-0000", "claim-0001", "claim-0002"]]
    # The measured payload IS the sent payload; enforcement cannot drift.
    assert gateway.payloads[0] == serialize_verifier_candidates(grounded(items))
    assert all(v.verdict == "verified" for v in verdicts)


def test_claim_count_split_is_deterministic_and_respects_both_bounds():
    items = refs(MAX_VERIFIER_CLAIMS_PER_BATCH + 1)
    gateway = RecordingGateway()
    verifier(gateway).verify(items)
    assert [len(batch) for batch in gateway.batches] == [MAX_VERIFIER_CLAIMS_PER_BATCH, 1]
    assert [claim for batch in gateway.batches for claim in batch] == \
        [item.claim_id for item in items]
    for payload, document in zip(gateway.payloads, gateway.documents):
        assert len(document["claims"]) <= MAX_VERIFIER_CLAIMS_PER_BATCH
        assert len(payload.encode("utf-8")) <= MAX_VERIFIER_BATCH_JSON_BYTES


def test_byte_size_split_happens_below_the_claim_count_limit():
    items = refs(4, value="v" * 9_000)
    assert len(items) < MAX_VERIFIER_CLAIMS_PER_BATCH
    assert verifier_payload_bytes(grounded(items)) > MAX_VERIFIER_BATCH_JSON_BYTES
    gateway = RecordingGateway()
    verifier(gateway).verify(items)
    assert gateway.batches == [["claim-0000", "claim-0001", "claim-0002"], ["claim-0003"]]
    for payload in gateway.payloads:
        assert len(payload.encode("utf-8")) <= MAX_VERIFIER_BATCH_JSON_BYTES


def test_single_oversized_candidate_fails_closed_before_any_provider_call():
    items = [ref(0), ref(1, value="v" * 40_000)]
    assert verifier_payload_bytes(grounded([items[1]])) > MAX_VERIFIER_BATCH_JSON_BYTES
    gateway = RecordingGateway()
    with pytest.raises(VerifierContractError) as excinfo:
        verifier(gateway).verify(items)
    assert excinfo.value.reason_code == "VERIFIER_CANDIDATE_TOO_LARGE"
    assert "v" * 100 not in str(excinfo.value)
    assert gateway.model_calls == 0  # the oversize batch is never sent


def test_three_batches_are_three_real_verifier_model_calls():
    gateway = RecordingGateway()
    verdicts = verifier(gateway).verify(refs(60))
    assert gateway.model_calls == 3
    assert [len(batch) for batch in gateway.batches] == [25, 25, 10]
    assert len(verdicts) == 60


def test_pure_partitioner_takes_explicit_bounds_and_never_calls_a_model():
    items = grounded(refs(5))
    batches = build_verifier_batches(items, max_claims=2, max_serialized_bytes=10_000)
    assert [[item.claim_id for item in batch] for batch in batches] == [
        ["claim-0000", "claim-0001"], ["claim-0002", "claim-0003"], ["claim-0004"]]


# --- F/G/K/L. ordering, exclusion and coverage -------------------------------

def test_shuffled_input_produces_identical_batches_and_verdict_order():
    items = refs(30)
    shuffled = list(items)
    random.Random(20260828).shuffle(shuffled)
    assert [item.claim_id for item in shuffled] != [item.claim_id for item in items]
    first, second = RecordingGateway(), RecordingGateway()
    ordered = verifier(first).verify(items)
    reordered = verifier(second).verify(shuffled)
    assert first.batches == second.batches
    assert first.payloads == second.payloads
    assert [v.model_dump(mode="json") for v in ordered] == \
        [v.model_dump(mode="json") for v in reordered]
    assert [v.claim_id for v in ordered] == sorted(v.claim_id for v in ordered)


def test_unsupported_and_conflicted_claims_never_appear_in_a_model_request():
    items = [ref(0), ref(1, supported=False), ref(2, entity="shared"),
             ref(3, entity="shared"), ref(4)]
    gateway = RecordingGateway()
    verdicts = verifier(gateway).verify(
        items, conflict_claim_ids={"claim-0002", "claim-0003"})
    assert gateway.batches == [["claim-0000", "claim-0004"]]
    sent = json.dumps(gateway.payloads)
    for excluded in ("claim-0001", "claim-0002", "claim-0003"):
        assert excluded not in sent
    assert {v.claim_id for v in verdicts} == {item.claim_id for item in items}


def test_reordered_response_is_accepted_and_final_order_stays_deterministic():
    def responder(claim_ids, _index, hashes):
        return {"verdicts": [{"claim_id": claim_id, "verdict": "verified",
                              "supporting_fragment_hashes": hashes[claim_id][:1]}
                             for claim_id in reversed(claim_ids)]}
    gateway = RecordingGateway(responder)
    verdicts = verifier(gateway).verify(refs(4))
    assert [v.claim_id for v in verdicts] == [
        "claim-0000", "claim-0001", "claim-0002", "claim-0003"]


def test_final_verdict_coverage_exactly_equals_evidence_coverage():
    items = [*refs(40), ref(90, supported=False), ref(91, entity="shared"),
             ref(92, entity="shared")]
    gateway = RecordingGateway()
    verdicts = verifier(gateway).verify(
        items, conflict_claim_ids={"claim-0091", "claim-0092"})
    claim_ids = [v.claim_id for v in verdicts]
    assert len(claim_ids) == len(set(claim_ids)) == len(items)
    assert set(claim_ids) == {item.claim_id for item in items}


def test_duplicate_evidence_identity_fails_closed():
    with pytest.raises(VerifierContractError) as excinfo:
        plan_grounded_verification([ref(0), ref(0)], resolver=StubResolver())
    assert excinfo.value.reason_code == "VERIFIER_EVIDENCE_DUPLICATE_CLAIM"


# --- H/I/J. batch-local omission and response identity -----------------------

def test_omission_is_local_to_its_own_batch():
    def responder(claim_ids, index, hashes):
        answered = claim_ids[1:] if index == 2 else claim_ids
        return {"verdicts": [{"claim_id": claim_id, "verdict": "verified",
                              "supporting_fragment_hashes": hashes[claim_id][:1]}
                             for claim_id in answered]}
    gateway = RecordingGateway(responder)
    verdicts = verifier(gateway).verify(refs(30))
    by_id = {v.claim_id: v for v in verdicts}
    omitted = gateway.batches[1][0]
    assert (by_id[omitted].verdict, by_id[omitted].reason) == ("rejected", "verifier omitted claim")
    assert all(by_id[claim_id].verdict == "verified" for claim_id in gateway.batches[0])
    assert all(by_id[claim_id].verdict == "verified" for claim_id in gateway.batches[1][1:])


def test_duplicate_response_claim_id_fails_closed_without_last_wins():
    def responder(claim_ids, _index, hashes):
        return {"verdicts": [
            {"claim_id": claim_ids[0], "verdict": "verified",
             "supporting_fragment_hashes": hashes[claim_ids[0]][:1]},
            {"claim_id": claim_ids[0], "verdict": "rejected"},
        ]}
    with pytest.raises(VerifierContractError) as excinfo:
        verifier(RecordingGateway(responder)).verify(refs(2))
    assert excinfo.value.reason_code == "VERIFIER_RESPONSE_DUPLICATE_CLAIM"


def test_foreign_response_claim_id_fails_closed():
    def responder(claim_ids, _index, hashes):
        return {"verdicts": [{"claim_id": f"foreign-{SENTINEL}", "verdict": "verified"}]}
    with pytest.raises(VerifierContractError) as excinfo:
        verifier(RecordingGateway(responder)).verify(refs(2))
    assert excinfo.value.reason_code == "VERIFIER_RESPONSE_UNKNOWN_CLAIM"
    assert SENTINEL not in str(excinfo.value)


def test_a_claim_from_another_batch_is_foreign_to_this_batch():
    with pytest.raises(VerifierContractError) as excinfo:
        parse_verifier_batch({"verdicts": [
            {"claim_id": "claim-0002", "verdict": "verified", "reason": "ok"}]},
            ["claim-0000", "claim-0001"],
            supporting_by_claim={"claim-0000": frozenset(), "claim-0001": frozenset()})
    assert excinfo.value.reason_code == "VERIFIER_RESPONSE_UNKNOWN_CLAIM"


@pytest.mark.parametrize("content", [
    f'{{"verdicts": [broken {SENTINEL}',
    {"verdicts": [{"claim_id": "claim-0000", "verdict": "maybe", "reason": SENTINEL}]},
    {"verdicts": {"claim-0000": SENTINEL}},
    [SENTINEL],
])
def test_malformed_response_shapes_fail_closed_without_raw_material(content):
    with pytest.raises(VerifierContractError) as excinfo:
        parse_verifier_batch(content, ["claim-0000"],
                             supporting_by_claim={"claim-0000": frozenset()})
    assert excinfo.value.reason_code == "VERIFIER_RESPONSE_INVALID"
    assert SENTINEL not in str(excinfo.value)
    assert SENTINEL not in excinfo.value.safe_message


# --- M. sequential execution -------------------------------------------------

def test_verifier_batches_run_strictly_sequentially():
    log: list[tuple[str, int]] = []
    gateway = RecordingGateway(log=log)
    verifier(gateway).verify(
        refs(60), batch_completed=lambda progress: log.append(("progress", progress.batch_index)))
    assert gateway.max_in_flight == 1
    assert log == [("progress", 0), ("call", 1), ("progress", 1),
                   ("call", 2), ("progress", 2), ("call", 3), ("progress", 3)]


# --- S/T/U. checkpoint compatibility ----------------------------------------

def test_empty_and_complete_checkpoints_both_remain_valid():
    items = refs(3)
    assert len(plan_grounded_verification(items, resolver=StubResolver(),
                                          existing_verdicts={}).batches) == 1
    complete = {item.claim_id: {"claim_id": item.claim_id, "verdict": "verified",
                                "reason": "ok"} for item in items}
    resumed = plan_grounded_verification(items, resolver=StubResolver(),
                                         existing_verdicts=complete)
    assert resumed.batches == ()
    assert [v.claim_id for v in resumed.settled] == [item.claim_id for item in items]


def test_malformed_checkpoint_verdict_fails_closed():
    with pytest.raises(VerifierContractError) as excinfo:
        plan_grounded_verification(refs(2), resolver=StubResolver(), existing_verdicts={
            "claim-0000": {"claim_id": "claim-0000", "verdict": "maybe", "reason": SENTINEL}})
    assert excinfo.value.reason_code == "VERIFIER_STATE_INVALID_VERDICT"
    assert SENTINEL not in str(excinfo.value)


def test_checkpoint_verdict_keyed_by_a_different_claim_fails_closed():
    with pytest.raises(VerifierContractError) as excinfo:
        plan_grounded_verification(refs(2), resolver=StubResolver(), existing_verdicts={
            "claim-0000": {"claim_id": "claim-0001", "verdict": "verified", "reason": "ok"}})
    assert excinfo.value.reason_code == "VERIFIER_STATE_INVALID_VERDICT"


def test_unknown_checkpoint_claim_fails_closed():
    with pytest.raises(VerifierContractError) as excinfo:
        plan_grounded_verification(refs(2), resolver=StubResolver(), existing_verdicts={
            "claim-9999": {"claim_id": "claim-9999", "verdict": "verified", "reason": "ok"}})
    assert excinfo.value.reason_code == "VERIFIER_STATE_UNKNOWN_CLAIM"


@pytest.mark.parametrize("stored,conflicts,unsupported", [
    ({"claim_id": "claim-0000", "verdict": "verified", "reason": "ok"}, {"claim-0000"}, False),
    ({"claim_id": "claim-0000", "verdict": "verified", "reason": "ok"}, set(), True),
    ({"claim_id": "claim-0000", "verdict": "needs_review", "reason": "other reason"},
     {"claim-0000"}, False),
])
def test_checkpoint_contradicting_a_deterministic_verdict_fails_closed(
        stored, conflicts, unsupported):
    items = [ref(0, supported=not unsupported, entity="shared"), ref(1, entity="shared")]
    with pytest.raises(VerifierContractError) as excinfo:
        plan_grounded_verification(items, resolver=StubResolver(),
                                   conflict_claim_ids=conflicts,
                                   existing_verdicts={"claim-0000": stored})
    assert excinfo.value.reason_code == "VERIFIER_STATE_INCOMPATIBLE_VERDICT"


# --- Q/V. resume at the Verifier boundary ------------------------------------

def test_resume_skips_checkpointed_claims_and_matches_an_uninterrupted_run():
    items = refs(60)
    uninterrupted_gateway = RecordingGateway()
    expected = verifier(uninterrupted_gateway).verify(items)
    assert uninterrupted_gateway.model_calls == 3

    first_batch = build_verifier_batches(grounded(items))[0]
    # Exactly what a completed grounded batch made durable: the reason is
    # backend-owned, so a resumed verdict is byte-identical to a fresh one.
    checkpoint = {item.claim_id: {"claim_id": item.claim_id, "verdict": "verified",
                                  "reason": GROUNDED_VERDICT_REASONS["verified"]}
                  for item in first_batch}
    resumed_gateway = RecordingGateway()
    resumed = verifier(resumed_gateway).verify(items, existing_verdicts=checkpoint)
    assert resumed_gateway.model_calls == 2
    called = {claim for batch in resumed_gateway.batches for claim in batch}
    assert called.isdisjoint(checkpoint)
    assert [v.model_dump(mode="json") for v in resumed] == \
        [v.model_dump(mode="json") for v in expected]


def test_an_uncheckpointed_batch_is_replayed_rather_than_falsely_skipped():
    """The provider-call -> checkpoint window is at-least-once, never
    exactly-once: a batch whose verdicts never became durable carries no
    marker that would let resume skip it."""
    items = refs(60)
    expected_batches = [[item.claim_id for item in batch]
                        for batch in build_verifier_batches(grounded(items))]
    durable: dict[str, dict] = {}

    def crash_after_the_second_batch(progress):
        if progress.batch_index == 2:
            raise RuntimeError("simulated crash before the checkpoint write")
        for verdict in progress.verdicts:
            durable[verdict.claim_id] = verdict.model_dump(mode="json")

    crashed = RecordingGateway()
    with pytest.raises(RuntimeError):
        verifier(crashed).verify(items, batch_completed=crash_after_the_second_batch)
    assert crashed.batches == expected_batches[:2]  # batch 2 WAS paid for
    assert set(durable) == set(expected_batches[0])  # and never became durable

    resumed_gateway = RecordingGateway()
    resumed = verifier(resumed_gateway).verify(items, existing_verdicts=durable)
    # Replayed, not skipped: the paid-but-uncheckpointed batch runs again.
    assert resumed_gateway.batches == expected_batches[1:]
    assert len(resumed) == 60


# --- engine integration: O/P/R/W and the real checkpoint path ----------------

def engine_with(gateway, evidence_refs, *, remaining=None, checkpoints=None,
                events=None, resolver=None):
    """One-task engine wired to offline fakes only.

    `remaining` receives the executor's call log, so a budget can change
    exactly at the boundary between plan pre-flight (log empty) and the
    exact pre-verifier check (the task has run).
    """
    calls: list[str] = []
    client = Plans(plan([task("a", "alpha")]),
                   [{"decision": "FINISH", "plan": None, "reason": "done"}])
    engine = SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker(calls), max_active_workers=1),
        verifier=verifier(gateway, resolver),
        evidence_loader=lambda results: (evidence_refs if "a" in results else []),
        usage_snapshot=gateway.snapshot,
        remaining_budget=(lambda: remaining(calls)) if remaining else (lambda: RemainingBudget(
            cost_units=1_000, tool_calls=100, tasks=10, model_calls=100)),
        checkpoint_sink=(lambda phase, value: checkpoints.append(deepcopy(value)))
        if checkpoints is not None else None,
        event_sink=(lambda kind, payload: events.append((kind, payload)))
        if events is not None else None)
    return engine, calls


def budget_after_execution(model_calls: int):
    """A generous plan pre-flight budget that tightens once the task has run."""
    return lambda calls: RemainingBudget(cost_units=1_000, tool_calls=100, tasks=10,
                                         model_calls=100 if not calls else model_calls)


def test_exact_pre_verifier_feasibility_stops_an_impossible_sequence():
    gateway = RecordingGateway()
    engine, calls = engine_with(gateway, refs(60), remaining=budget_after_execution(2))
    with pytest.raises(ValueError, match="verification exceeds remaining model-call budget"):
        engine.run({"id": "run-1", "input": {"objective": "batching",
                                             "commander_model": "fake"}})
    assert calls == ["a"]
    assert gateway.model_calls == 0  # three batches required, two slots left


def test_exact_pre_verifier_feasibility_passes_with_exactly_enough_capacity():
    gateway = RecordingGateway()
    engine, _ = engine_with(gateway, refs(60), remaining=budget_after_execution(3))
    result = engine.run({"id": "run-1", "input": {"objective": "batching",
                                                  "commander_model": "fake"}})
    assert gateway.model_calls == 3
    assert result["status"] == "complete"


def test_each_completed_batch_checkpoints_verdicts_usage_and_emits_progress():
    gateway, checkpoints, events = RecordingGateway(), [], []
    engine, _ = engine_with(gateway, [*refs(30), ref(90, supported=False)],
                            checkpoints=checkpoints, events=events)
    result = engine.run({"id": "run-1", "input": {"objective": "batching",
                                                  "commander_model": "fake"}})
    verification = [c for c in checkpoints if c["artifacts"]["swarm_state"]["verifier_state"]]
    # deterministic verdicts, batch 1, batch 2 and the final verdict map
    assert len(verification) == 4
    states = [c["artifacts"]["swarm_state"] for c in verification]
    assert set(states[0]["verifier_state"]) == {"claim-0090"}
    assert states[0]["usage_snapshot"] == {"model_calls": 0}
    assert set(states[1]["verifier_state"]) == {"claim-0090", *gateway.batches[0]}
    assert states[1]["usage_snapshot"] == {"model_calls": 1}
    assert set(states[2]["verifier_state"]) == {
        "claim-0090", *gateway.batches[0], *gateway.batches[1]}
    assert states[2]["usage_snapshot"] == {"model_calls": 2}
    assert set(states[-1]["verifier_state"]) == {f"claim-{i:04d}" for i in range(30)} | {"claim-0090"}
    progress = [payload for kind, payload in events if kind == "verification_batch_completed"]
    assert progress == [{"batch_index": 1, "batch_count": 2, "claim_count": 25},
                        {"batch_index": 2, "batch_count": 2, "claim_count": 5}]
    assert ("verification_completed", {"status": "completed"}) in events
    assert result["status"] == "partial_success"  # the unsupported claim is rejected


def test_engine_resume_skips_checkpointed_verifier_claims():
    evidence_refs = refs(60, task_id="a")
    uninterrupted_gateway, uninterrupted_checkpoints = RecordingGateway(), []
    engine, _ = engine_with(uninterrupted_gateway, evidence_refs,
                            checkpoints=uninterrupted_checkpoints)
    expected = engine.run({"id": "run-1", "input": {"objective": "batching",
                                                    "commander_model": "fake"}})
    assert uninterrupted_gateway.model_calls == 3

    # The exact durable state after the FIRST completed verifier batch.
    partial = next(deepcopy(c) for c in uninterrupted_checkpoints
                   if len(c["artifacts"]["swarm_state"]["verifier_state"]) == 25)
    saved = partial["artifacts"]["swarm_state"]
    assert set(saved["verifier_state"]) == set(uninterrupted_gateway.batches[0])

    resumed_gateway, resumed_checkpoints = RecordingGateway(), []
    engine, calls = engine_with(resumed_gateway, evidence_refs,
                                checkpoints=resumed_checkpoints)
    result = engine.run({"id": "run-1", "input": {"commander_model": "fake"},
                         "checkpoint": {"artifacts": {"swarm_state": saved}}})
    assert calls == []  # the completed task did not rerun
    assert resumed_gateway.model_calls == 2
    called = {claim for batch in resumed_gateway.batches for claim in batch}
    assert called.isdisjoint(saved["verifier_state"])
    assert result == expected
    assert (resumed_checkpoints[-1]["artifacts"]["swarm_state"]["verifier_state"] ==
            uninterrupted_checkpoints[-1]["artifacts"]["swarm_state"]["verifier_state"])


def test_incompatible_checkpoint_verifier_state_fails_the_run_closed():
    evidence_refs = refs(3)
    gateway, checkpoints = RecordingGateway(), []
    state = SwarmState(run_id="run-1", objective="resume",
        approved_plan=plan([task("a", "alpha")]), completed_task_ids=["a"],
        task_outputs={"a": {"answer": "a"}},
        evidence_references=[item.model_dump(mode="json") for item in evidence_refs],
        verifier_state={"claim-9999": {"claim_id": "claim-9999", "verdict": "verified",
                                       "reason": "stale"}})
    engine, _ = engine_with(gateway, evidence_refs, checkpoints=checkpoints)
    with pytest.raises(VerifierContractError) as excinfo:
        engine.run({"id": "run-1", "input": {"commander_model": "fake"},
                    "checkpoint": {"artifacts": {"swarm_state": state.model_dump(mode="json")}}})
    assert excinfo.value.reason_code == "VERIFIER_STATE_UNKNOWN_CLAIM"
    assert gateway.model_calls == 0


def test_no_raw_verifier_material_reaches_state_events_or_the_error():
    def responder(claim_ids, _index, hashes):
        return {"verdicts": [{"claim_id": f"{SENTINEL}", "verdict": "verified"}]}
    gateway, checkpoints, events = RecordingGateway(responder), [], []
    engine, _ = engine_with(gateway, refs(3), checkpoints=checkpoints, events=events)
    with pytest.raises(VerifierContractError) as excinfo:
        engine.run({"id": "run-1", "input": {"objective": "hygiene",
                                             "commander_model": "fake"}})
    assert excinfo.value.reason_code == "VERIFIER_RESPONSE_UNKNOWN_CLAIM"
    assert SENTINEL not in str(excinfo.value)
    assert SENTINEL not in json.dumps(checkpoints)
    assert SENTINEL not in json.dumps(events)
    assert all(not c["artifacts"]["swarm_state"]["verifier_state"] for c in checkpoints)
